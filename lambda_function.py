import json
import os
import re
import datetime
import hashlib
import requests
import google.generativeai as genai

# --- ENVIRONMENT VARIABLES ---
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
JIRA_USERNAME = os.environ["JIRA_USERNAME"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
JIRA_DOMAIN = os.environ["JIRA_DOMAIN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
# Use a valid model name with fallback
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
# Map old model names to new ones
MODEL_MAPPING = {
    "gemini-pro": "gemini-1.5-pro",
    "gemini-pro-vision": "gemini-1.5-pro",
}
if GEMINI_MODEL in MODEL_MAPPING:
    GEMINI_MODEL = MODEL_MAPPING[GEMINI_MODEL]
    print(f"Mapped model to: {GEMINI_MODEL}")

JIRA_HOSPITAL_FIELD = os.environ.get("JIRA_HOSPITAL_FIELD", "customfield_12345")
JIRA_SUMMARY_FIELD = "customfield_10250"

# --- DEDUPLICATION CACHE ---
# Simple in-memory cache for deduplication (resets on each Lambda cold start)
processed_events = set()
MAX_CACHE_SIZE = 1000  # Prevent memory issues in long-running containers

def add_to_cache(event_id):
    """Add event to cache with size management"""
    global processed_events
    
    # If cache is getting too large, clear oldest half
    if len(processed_events) >= MAX_CACHE_SIZE:
        print(f"Cache size limit reached ({MAX_CACHE_SIZE}), clearing oldest entries")
        # Convert to list, keep newest half, convert back to set
        events_list = list(processed_events)
        processed_events = set(events_list[len(events_list)//2:])
    
    processed_events.add(event_id)
    print(f"Added to cache: {event_id} (cache size: {len(processed_events)})")

# --- CLIENTS AND HEADERS ---
# Configure Gemini client globally
genai.configure(api_key=GEMINI_API_KEY)

SLACK_HEADERS = {
    "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
    "Content-Type": "application/json"
}

# --- LAMBDA HANDLER ---
def lambda_handler(event, context=None):
    try:
        print("Incoming event:", json.dumps(event))
        if event.get("body"):
            body = json.loads(event["body"])

            if body.get("type") == "url_verification":
                return {
                    "statusCode": 200,
                    "body": body.get("challenge")
                }

            if body.get("type") == "event_callback":
                # Check for duplicate events
                event_data = body.get("event", {})
                event_id = create_event_id(event_data)
                
                if event_id in processed_events:
                    print(f"Duplicate event detected, skipping: {event_id}")
                    return {"statusCode": 200, "body": "Duplicate event skipped"}
                
                # Mark event as processed
                add_to_cache(event_id)
                print(f"Processing new event: {event_id}")
                
                user_id = event_data.get("user")
                try:
                    process_fire_ticket(body, user_id)
                except Exception as err:
                    print("Error during fire ticket processing:", err)
                    # Remove from processed events if processing failed
                    processed_events.discard(event_id)
                    return {"statusCode": 500, "body": str(err)}
                return {"statusCode": 200, "body": "OK"}

        return {"statusCode": 400, "body": "Bad request"}

    except Exception as e:
        print("Unhandled exception in lambda_handler:", e)
        return {"statusCode": 500, "body": str(e)}

def create_event_id(event_data):
    """Create a unique identifier for deduplication"""
    # Use timestamp, channel, user, and text to create unique ID
    timestamp = event_data.get("ts", "")
    channel = event_data.get("channel", "")
    user = event_data.get("user", "")
    text = event_data.get("text", "")
    
    # Create a hash-like identifier
    unique_string = f"{timestamp}_{channel}_{user}_{text}"
    return hashlib.md5(unique_string.encode()).hexdigest()[:16]

# --- CORE LOGIC ---
def process_fire_ticket(event_data, user_id):
    text = event_data["event"].get("text", "")
    print(f"Processing message: {text}")
    
    issue_match = re.search(r"(ISD-\d{5})", text)
    if not issue_match:
        print("No Jira issue key found in text:", text)
        return

    issue_key = issue_match.group(1)
    print(f"Found Jira issue: {issue_key}")
    
    try:
        jira_data = fetch_jira_data(issue_key)
        if jira_data.status_code != 200:
            raise Exception(f"Failed to fetch Jira ticket data: {jira_data.text}")

        ticket = jira_data.json()
        print(f"Successfully fetched Jira ticket: {issue_key}")
        
        parsed_data = parse_jira_ticket(ticket)
        print(f"Parsed ticket data - Summary length: {len(parsed_data['summary'])}, Description length: {len(parsed_data['description'])}")
        
        summary = generate_gemini_summary(parsed_data)
        print(f"Generated summary length: {len(summary)}")

        date_str = datetime.datetime.now().strftime("%Y%m%d")
        channel_slug = issue_key.lower()
        base_channel_name = f"incident-{channel_slug}-{date_str}"
        
        channel_id, channel_name = create_incident_channel(base_channel_name)
        print(f"Created/found channel: {channel_name} ({channel_id})")

        invite_user_to_channel(user_id, channel_id)
        post_welcome_message(event_data["event"]["channel"], channel_name, channel_id)
        post_summary_message(channel_id, summary)
        
        print(f"Successfully processed fire ticket for {issue_key}")
        
    except Exception as e:
        print(f"Error processing fire ticket {issue_key}: {e}")
        raise

# --- JIRA AND GEMINI FUNCTIONS ---
def fetch_jira_data(issue_key):
    url = f"https://{JIRA_DOMAIN}/rest/api/3/issue/{issue_key}"
    print(f"Fetching Jira ticket from URL: {url}")
    response = requests.get(
        url,
        auth=(JIRA_USERNAME, JIRA_API_TOKEN),
        headers={"Accept": "application/json"}
    )
    print("Jira response status:", response.status_code)
    return response

def parse_jira_ticket(ticket):
    fields = ticket.get("fields", {})
    summary = fields.get(JIRA_SUMMARY_FIELD, "")
    
    # Handle Jira description which might be in ADF format
    description_field = fields.get("description", "")
    description = ""
    
    if isinstance(description_field, dict):
        # ADF format - extract text content
        description = extract_text_from_adf(description_field)
    elif isinstance(description_field, str):
        # Plain text
        description = description_field
    
    return {"summary": summary, "description": description}

def extract_text_from_adf(adf_content):
    """Extract plain text from Atlassian Document Format (ADF)"""
    if not isinstance(adf_content, dict):
        return str(adf_content)
    
    text_parts = []
    
    def extract_text_recursive(node):
        if isinstance(node, dict):
            # If it's a text node, extract the text
            if node.get("type") == "text":
                text_parts.append(node.get("text", ""))
            
            # Recursively process content and other children
            for key in ["content", "marks", "attrs"]:
                if key in node and isinstance(node[key], (list, dict)):
                    if isinstance(node[key], list):
                        for item in node[key]:
                            extract_text_recursive(item)
                    else:
                        extract_text_recursive(node[key])
        elif isinstance(node, list):
            for item in node:
                extract_text_recursive(item)
    
    extract_text_recursive(adf_content)
    return " ".join(text_parts).strip()

def generate_gemini_summary(data):
    """Generates a summary of a Jira ticket using the Gemini API."""
    fallback_models = ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-pro"]
    models_to_try = [GEMINI_MODEL] + [m for m in fallback_models if m != GEMINI_MODEL]
    
    for model_name in models_to_try:
        try:
            print(f"Trying Gemini model: {model_name}")
            model = genai.GenerativeModel(model_name)
            prompt = f"""You are a helpful assistant summarizing incident tickets.

Summary:
{data['summary']}

Description:
{data['description']}

Please provide a concise summary in plain English suitable for a Slack incident channel."""

            response = model.generate_content(prompt)
            
            # Updated response handling for current API
            if hasattr(response, 'text') and response.text:
                print(f"Successfully generated summary with model: {model_name}")
                return response.text.strip()
            elif response.parts:
                summary_text = ''.join(part.text for part in response.parts if hasattr(part, 'text'))
                if summary_text:
                    print(f"Successfully generated summary with model: {model_name}")
                    return summary_text.strip()
            
            print(f"Empty response from model: {model_name}")
            
        except Exception as e:
            print(f"Error with model {model_name}: {e}")
            if model_name == models_to_try[-1]:  # Last model failed
                return "Gemini summary could not be generated due to an error."
            continue  # Try next model
    
    return "Gemini summary could not be generated."

# --- SLACK HELPER FUNCTIONS ---
def create_incident_channel(base_name):
    original_name = base_name.lower()

    response = requests.get(
        "https://slack.com/api/conversations.list",
        headers=SLACK_HEADERS,
        params={"exclude_archived": "false", "limit": 1000}
    ).json()

    if not response.get("ok"):
        raise Exception(f"Failed to list Slack channels: {response}")

    existing_channels = {c["name"]: c for c in response.get("channels", [])}

    if original_name in existing_channels:
        channel = existing_channels[original_name]
        if not channel.get("is_archived"):
            print(f"Reusing active channel: {original_name}")
            return channel["id"], original_name
        else:
            # Handle archived channels by creating numbered versions
            print(f"Channel {original_name} is archived, finding next available numbered version")
            counter = 1
            while True:
                numbered_name = f"{original_name}-{counter}"
                if numbered_name in existing_channels:
                    if not existing_channels[numbered_name].get("is_archived"):
                        print(f"Reusing active numbered channel: {numbered_name}")
                        return existing_channels[numbered_name]["id"], numbered_name
                    counter += 1
                else:
                    # Create the numbered channel
                    print(f"Creating new numbered channel: {numbered_name}")
                    create_response = requests.post(
                        "https://slack.com/api/conversations.create",
                        headers=SLACK_HEADERS,
                        json={"name": numbered_name, "is_private": False}
                    ).json()
                    if create_response.get("ok"):
                        return create_response["channel"]["id"], numbered_name
                    else:
                        raise Exception(f"Failed to create numbered channel: {create_response.get('error')}")

    print(f"Creating new channel: {original_name}")
    create_response = requests.post(
        "https://slack.com/api/conversations.create",
        headers=SLACK_HEADERS,
        json={"name": original_name, "is_private": False}
    ).json()

    if create_response.get("ok"):
        return create_response["channel"]["id"], original_name
    else:
        raise Exception(f"Failed to create channel: {create_response.get('error')}")

def invite_user_to_channel(user_id, channel_id):
    response = requests.post(
        "https://slack.com/api/conversations.invite",
        headers=SLACK_HEADERS,
        json={"channel": channel_id, "users": user_id}
    ).json()
    if not response.get("ok"):
        print(f"Warning: Could not invite user {user_id} to {channel_id}: {response.get('error')}")

def post_welcome_message(source_channel, new_channel_name, new_channel_id):
    response = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers=SLACK_HEADERS,
        json={
            "channel": source_channel,
            "text": f":rotating_light: Incident channel <#{new_channel_id}|{new_channel_name}> has been created. Please move all communications there. :rotating_light:"
        }
    ).json()
    if not response.get("ok"):
        print(f"Error posting welcome message: {response.get('error')}")

def post_summary_message(channel_id, summary):
    response = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers=SLACK_HEADERS,
        json={"channel": channel_id, "text": f"*Incident Summary:*\n{summary}"}
    ).json()
    if not response.get("ok"):
        print(f"Error posting summary message: {response.get('error')}")
