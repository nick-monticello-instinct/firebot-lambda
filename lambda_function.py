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

# --- SLACK PERMISSIONS REQUIRED ---
# The following Slack OAuth scopes are required for full functionality:
# - channels:read          (list channels)
# - channels:write         (create channels)
# - channels:manage        (invite users to channels)
# - chat:write            (post messages)
# - users:read.email      (lookup users by email - required for creator outreach)
# - users:read            (get user information)

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
                
                print(f"Current cache contents: {list(processed_events)}")
                print(f"Checking if event {event_id} is already processed...")
                
                if event_id in processed_events:
                    print(f"❌ Duplicate event detected, skipping: {event_id}")
                    return {"statusCode": 200, "body": "Duplicate event skipped"}
                
                # Mark event as processed
                print(f"✅ New event detected: {event_id}")
                add_to_cache(event_id)
                
                user_id = event_data.get("user")
                try:
                    process_fire_ticket(body, user_id)
                except Exception as err:
                    print("Error during fire ticket processing:", err)
                    # Remove from processed events if processing failed
                    processed_events.discard(event_id)
                    print(f"Removed failed event from cache: {event_id}")
                    return {"statusCode": 500, "body": str(err)}
                return {"statusCode": 200, "body": "OK"}

        return {"statusCode": 400, "body": "Bad request"}

    except Exception as e:
        print("Unhandled exception in lambda_handler:", e)
        return {"statusCode": 500, "body": str(e)}

def create_event_id(event_data):
    """Create a unique identifier for deduplication"""
    # Use channel, user, and Jira issue key for deduplication
    # This is more stable than timestamp which might vary slightly
    channel = event_data.get("channel", "")
    user = event_data.get("user", "")
    text = event_data.get("text", "")
    
    # Extract Jira issue key from text for more targeted deduplication
    issue_match = re.search(r"(ISD-\d{5})", text)
    issue_key = issue_match.group(1) if issue_match else ""
    
    # Create identifier based on what really matters: user + channel + issue
    unique_string = f"{channel}_{user}_{issue_key}"
    event_id = hashlib.md5(unique_string.encode()).hexdigest()[:16]
    
    # Log for debugging
    print(f"Event deduplication - Channel: {channel}, User: {user}, Issue: {issue_key}")
    print(f"Generated event ID: {event_id}")
    
    return event_id

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
    
    # Check if this incident has already been processed (persistent check across Lambda executions)
    if check_incident_already_processed(issue_key):
        print(f"Incident {issue_key} has already been processed recently, skipping")
        return
    
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
        
        # NEW: Analyze ticket for missing information and reach out to creator
        try:
            analyze_and_reach_out_to_creator(ticket, channel_id, issue_key)
        except Exception as e:
            print(f"Error in ticket analysis and outreach: {e}")
            # Don't fail the entire process if this step fails
        
        print(f"Successfully processed fire ticket for {issue_key}")
        
    except Exception as e:
        print(f"Error processing fire ticket {issue_key}: {e}")
        raise

def analyze_and_reach_out_to_creator(ticket, channel_id, issue_key):
    """Analyze ticket for missing info and reach out to creator"""
    print(f"Analyzing ticket {issue_key} for missing information...")
    
    # Extract creator information from Jira
    creator_info = extract_creator_info(ticket)
    if not creator_info:
        print("Could not extract creator information from ticket")
        return
    
    # Analyze ticket for missing information
    parsed_data = parse_jira_ticket(ticket)
    missing_info_analysis = analyze_missing_information(parsed_data, ticket)
    
    # Post general analysis to channel for all responders
    post_incident_analysis_message(channel_id, missing_info_analysis, issue_key)
    
    # Find creator in Slack
    slack_user_id = find_slack_user_by_email(creator_info.get('email'))
    
    # Invite creator to the incident channel if found in Slack
    if slack_user_id:
        print(f"Inviting ticket creator {slack_user_id} to incident channel")
        invite_user_to_channel(slack_user_id, channel_id)
    
    # Generate and send outreach message
    outreach_message = generate_creator_outreach_message(
        creator_info, 
        missing_info_analysis, 
        issue_key,
        slack_user_id
    )
    
    post_creator_outreach_message(channel_id, outreach_message, slack_user_id)

def extract_creator_info(ticket):
    """Extract creator/reporter information from Jira ticket"""
    try:
        fields = ticket.get("fields", {})
        reporter = fields.get("reporter", {})
        
        creator_info = {
            "display_name": reporter.get("displayName", ""),
            "email": reporter.get("emailAddress", ""),
            "account_id": reporter.get("accountId", ""),
            "username": reporter.get("name", "")  # May not be available in newer Jira
        }
        
        print(f"Extracted creator info: {creator_info}")
        return creator_info
        
    except Exception as e:
        print(f"Error extracting creator info: {e}")
        return None

def analyze_missing_information(parsed_data, full_ticket):
    """Use Gemini to analyze what information might be missing from the incident"""
    try:
        # Get additional fields that might be relevant
        fields = full_ticket.get("fields", {})
        priority = fields.get("priority", {}).get("name", "Unknown")
        status = fields.get("status", {}).get("name", "Unknown")
        created = fields.get("created", "Unknown")
        
        # Create a comprehensive analysis prompt
        prompt = f"""You are an expert incident response analyst. Analyze this Jira incident ticket and identify what critical information might be missing for effective incident resolution.

TICKET DETAILS:
Priority: {priority}
Status: {status}
Created: {created}

Summary: {parsed_data['summary']}

Description: {parsed_data['description']}

Please analyze this incident and identify:
1. What critical information is missing that would help developers resolve this faster?
2. What additional context would be valuable?
3. Are there any obvious gaps in the incident report?

Focus on practical, actionable information like:
- Reproduction steps
- Error messages/logs
- Environment details (prod/staging/dev)
- Impact assessment (how many users affected)
- Recent changes or deployments
- Screenshots or examples
- Urgency context

Provide a concise analysis in a helpful, professional tone. Be specific about what's missing rather than generic."""

        fallback_models = ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-pro"]
        models_to_try = [GEMINI_MODEL] + [m for m in fallback_models if m != GEMINI_MODEL]
        
        for model_name in models_to_try:
            try:
                print(f"Analyzing missing information with model: {model_name}")
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(prompt)
                
                if hasattr(response, 'text') and response.text:
                    print(f"Successfully analyzed missing information with model: {model_name}")
                    return response.text.strip()
                elif response.parts:
                    analysis_text = ''.join(part.text for part in response.parts if hasattr(part, 'text'))
                    if analysis_text:
                        print(f"Successfully analyzed missing information with model: {model_name}")
                        return analysis_text.strip()
                
            except Exception as e:
                print(f"Error with model {model_name}: {e}")
                continue
        
        return "Could not analyze missing information due to AI service issues."
        
    except Exception as e:
        print(f"Error analyzing missing information: {e}")
        return "Could not analyze missing information due to an error."

def find_slack_user_by_email(email):
    """Find Slack user ID by email address"""
    if not email:
        return None
        
    try:
        response = requests.get(
            "https://slack.com/api/users.lookupByEmail",
            headers=SLACK_HEADERS,
            params={"email": email}
        ).json()
        
        if response.get("ok"):
            user_id = response.get("user", {}).get("id")
            print(f"Found Slack user {user_id} for email {email}")
            return user_id
        else:
            print(f"Could not find Slack user for email {email}: {response.get('error')}")
            return None
            
    except Exception as e:
        print(f"Error finding Slack user by email: {e}")
        return None

def generate_creator_outreach_message(creator_info, missing_info_analysis, issue_key, slack_user_id):
    """Generate a personalized outreach message for the ticket creator"""
    try:
        creator_name = creator_info.get("display_name", "").split()[0] if creator_info.get("display_name") else "there"
        user_mention = f"<@{slack_user_id}>" if slack_user_id else creator_name
        
        prompt = f"""You are a helpful incident response bot. Generate a professional, friendly message to reach out to the person who created incident ticket {issue_key}.

CREATOR INFO:
Name: {creator_name}

MISSING INFORMATION ANALYSIS:
{missing_info_analysis}

Create a message that:
1. Greets them professionally and thanks them for reporting the incident
2. Lets them know a developer is on the way to help
3. Mentions the specific information that would be helpful (based on the analysis)
4. Asks if they have any additional context that might speed up resolution
5. Is encouraging and supportive (incidents can be stressful)

Keep it conversational but professional. Use their first name if available. Make it clear this is automated assistance to help get them faster resolution.

The message should be suitable for posting in a Slack channel. Don't include channel mentions or formatting beyond basic Slack markdown."""

        fallback_models = ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-pro"]
        models_to_try = [GEMINI_MODEL] + [m for m in fallback_models if m != GEMINI_MODEL]
        
        for model_name in models_to_try:
            try:
                print(f"Generating outreach message with model: {model_name}")
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(prompt)
                
                if hasattr(response, 'text') and response.text:
                    message = response.text.strip()
                    # Add user mention at the beginning
                    final_message = f"{user_mention} {message}"
                    print(f"Successfully generated outreach message with model: {model_name}")
                    return final_message
                elif response.parts:
                    message_text = ''.join(part.text for part in response.parts if hasattr(part, 'text'))
                    if message_text:
                        final_message = f"{user_mention} {message_text.strip()}"
                        print(f"Successfully generated outreach message with model: {model_name}")
                        return final_message
                
            except Exception as e:
                print(f"Error with model {model_name}: {e}")
                continue
        
        # Fallback message if AI fails
        return f"{user_mention} Hi {creator_name}! Thanks for reporting incident {issue_key}. A developer is on the way to help. If you have any additional details, error messages, or context that might help us resolve this faster, please share them here. We're working to get this resolved as quickly as possible!"
        
    except Exception as e:
        print(f"Error generating outreach message: {e}")
        return f"Hi! Thanks for reporting incident {issue_key}. A developer is on the way to help. Please share any additional details that might help us resolve this faster."

def post_creator_outreach_message(channel_id, message, slack_user_id):
    """Post the outreach message to the incident channel"""
    try:
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers=SLACK_HEADERS,
            json={
                "channel": channel_id,
                "text": message,
                "unfurl_links": False,
                "unfurl_media": False
            }
        ).json()
        
        if response.get("ok"):
            print("Successfully posted creator outreach message")
        else:
            print(f"Error posting creator outreach message: {response.get('error')}")
            
    except Exception as e:
        print(f"Error posting creator outreach message: {e}")

def post_incident_analysis_message(channel_id, analysis, issue_key):
    """Post general incident analysis to channel for all responders"""
    try:
        analysis_message = f":mag: **Incident Analysis for {issue_key}**\n\n{analysis}\n\n*This analysis was automatically generated to help responders understand what information might be needed for faster resolution.*"
        
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers=SLACK_HEADERS,
            json={
                "channel": channel_id,
                "text": analysis_message,
                "unfurl_links": False,
                "unfurl_media": False
            }
        ).json()
        
        if response.get("ok"):
            print("Successfully posted incident analysis message")
        else:
            print(f"Error posting incident analysis message: {response.get('error')}")
            
    except Exception as e:
        print(f"Error posting incident analysis message: {e}")

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

def check_incident_already_processed(issue_key):
    """Check if this incident has already been processed by looking for existing active channel"""
    try:
        date_str = datetime.datetime.now().strftime("%Y%m%d")
        base_channel_name = f"incident-{issue_key.lower()}-{date_str}"
        
        response = requests.get(
            "https://slack.com/api/conversations.list",
            headers=SLACK_HEADERS,
            params={"exclude_archived": "false", "limit": 1000}
        ).json()

        if not response.get("ok"):
            print(f"Warning: Could not check existing channels: {response}")
            return False

        existing_channels = {c["name"]: c for c in response.get("channels", [])}
        
        # Check if base channel or any numbered version exists and is active
        for channel_name, channel in existing_channels.items():
            if (channel_name == base_channel_name or 
                channel_name.startswith(f"{base_channel_name}-")):
                if not channel.get("is_archived"):
                    print(f"Found existing active channel: {channel_name}")
                    # Check if channel has recent activity (last 5 minutes)
                    channel_id = channel["id"]
                    messages = get_recent_channel_messages(channel_id)
                    if messages:
                        print(f"Channel {channel_name} has recent activity, incident already processed")
                        return True
        
        return False
        
    except Exception as e:
        print(f"Error checking if incident already processed: {e}")
        return False

def get_recent_channel_messages(channel_id):
    """Get messages from the last 5 minutes to check for recent activity"""
    try:
        # Get messages from last 5 minutes
        five_minutes_ago = datetime.datetime.now() - datetime.timedelta(minutes=5)
        oldest_timestamp = five_minutes_ago.timestamp()
        
        response = requests.get(
            "https://slack.com/api/conversations.history",
            headers=SLACK_HEADERS,
            params={
                "channel": channel_id,
                "oldest": oldest_timestamp,
                "limit": 10
            }
        ).json()
        
        if response.get("ok"):
            return response.get("messages", [])
        else:
            print(f"Warning: Could not get channel history: {response}")
            return []
            
    except Exception as e:
        print(f"Error getting channel messages: {e}")
        return []
