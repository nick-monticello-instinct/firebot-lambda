import json
import os
import re
import datetime
import hashlib
import requests
import google.generativeai as genai
import mimetypes
from io import BytesIO
try:
    from PIL import Image
except ImportError:
    print("Warning: Pillow not available, image validation will be limited")
    Image = None

# --- ENVIRONMENT VARIABLES ---
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
JIRA_USERNAME = os.environ["JIRA_USERNAME"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
JIRA_DOMAIN = os.environ["JIRA_DOMAIN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
# Optional: SLACK_BOT_USER_ID can be set to help prevent duplicate processing
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

JIRA_HOSPITAL_FIELD = os.environ.get("JIRA_HOSPITAL_FIELD", "customfield_10297")
JIRA_SUMMARY_FIELD = "customfield_10250"

# --- SLACK PERMISSIONS REQUIRED ---
# The following Slack OAuth scopes are required for full functionality:
# - channels:read          (list channels)
# - channels:write         (create channels)
# - channels:manage        (invite users to channels)
# - chat:write            (post messages)
# - users:read.email      (lookup users by email - required for creator outreach)
# - users:read            (get user information)
# - files:write           (upload media files from Jira attachments)
# - files:read            (read file information for error handling)

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
    # Use channel, user, timestamp, and Jira issue key for deduplication
    channel = event_data.get("channel", "")
    user = event_data.get("user", "")
    text = event_data.get("text", "")
    timestamp = event_data.get("ts", "")  # Slack event timestamp
    
    # Extract Jira issue key from text for more targeted deduplication
    issue_match = re.search(r"(ISD-\d{5})", text)
    issue_key = issue_match.group(1) if issue_match else ""
    
    # Create identifier based on what really matters: user + channel + issue + timestamp
    unique_string = f"{channel}_{user}_{issue_key}_{timestamp}"
    event_id = hashlib.md5(unique_string.encode()).hexdigest()[:16]
    
    # Log for debugging
    print(f"Event deduplication - Channel: {channel}, User: {user}, Issue: {issue_key}, Timestamp: {timestamp}")
    print(f"Generated event ID: {event_id}")
    print(f"Current cache size: {len(processed_events)}")
    print(f"Current cache contents: {list(processed_events)}")
    
    return event_id

# --- CORE LOGIC ---
def process_fire_ticket(event_data, user_id):
    event = event_data["event"]
    text = event.get("text", "")
    print(f"Processing message: {text}")
    
    # Skip messages from bots to prevent processing our own messages
    if event.get("bot_id") or event.get("app_id"):
        print("Skipping bot message to prevent duplicate processing")
        return
    
    # Additional check: skip if the message is from our specific bot user
    bot_user_ids = [os.environ.get("SLACK_BOT_USER_ID"), "U09584DT15X"]  # Add known bot user ID as fallback
    if user_id in [uid for uid in bot_user_ids if uid]:
        print(f"Skipping message from bot user {user_id} to prevent duplicate processing")
        return
    
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

        # Extract hospital name and format for channel name
        hospital_name = extract_hospital_name(ticket)
        hospital_slug = format_hospital_for_channel(hospital_name)
        
        date_str = datetime.datetime.now().strftime("%Y%m%d")
        channel_slug = issue_key.lower()
        base_channel_name = f"incident-{channel_slug}-{date_str}-{hospital_slug}"
        
        channel_id, channel_name = create_incident_channel(base_channel_name)
        print(f"Created/found channel: {channel_name} ({channel_id})")

        invite_user_to_channel(user_id, channel_id)
        post_welcome_message(event_data["event"]["channel"], channel_name, channel_id)
        
        # Check if we've already posted the summary for this incident
        summary_cache_key = f"summary_{issue_key}"
        if summary_cache_key not in processed_events:
            processed_events.add(summary_cache_key)
            post_summary_message(channel_id, summary)
        else:
            print(f"Summary for {issue_key} already posted, skipping")
        
        # NEW: Analyze ticket for missing information and reach out to creator
        try:
            analyze_and_reach_out_to_creator(ticket, channel_id, issue_key)
        except Exception as e:
            print(f"Error in ticket analysis and outreach: {e}")
            # Don't fail the entire process if this step fails
        
        # NEW: Process media attachments from Jira ticket
        try:
            media_cache_key = f"media_{issue_key}"
            if media_cache_key not in processed_events:
                processed_events.add(media_cache_key)
                
                print(f"Processing media attachments for {issue_key}")
                attachments = fetch_jira_attachments(issue_key)
                
                if attachments:
                    print(f"Found {len(attachments)} media attachments, processing...")
                    media_files = download_and_process_media(attachments)
                    
                    if media_files:
                        uploaded_files = upload_media_to_slack(media_files, channel_id, issue_key)
                        post_media_summary(channel_id, uploaded_files, issue_key)
                        print(f"Successfully processed {len(uploaded_files)} media files for {issue_key}")
                    else:
                        print(f"No valid media files to upload for {issue_key}")
                else:
                    print(f"No media attachments found for {issue_key}")
            else:
                print(f"Media for {issue_key} already processed, skipping")
                
        except Exception as e:
            print(f"Error in media processing for {issue_key}: {e}")
            # Don't fail the entire process if media processing fails
            processed_events.discard(f"media_{issue_key}")  # Allow retry on next run
        
        print(f"Successfully processed fire ticket for {issue_key}")
        
    except Exception as e:
        print(f"Error processing fire ticket {issue_key}: {e}")
        raise

def analyze_and_reach_out_to_creator(ticket, channel_id, issue_key):
    """Analyze ticket for missing info and reach out to creator"""
    print(f"Analyzing ticket {issue_key} for missing information...")
    
    # Check if we've already processed this incident's analysis
    analysis_cache_key = f"analysis_{issue_key}"
    if analysis_cache_key in processed_events:
        print(f"Analysis for {issue_key} already completed, skipping")
        return
    
    # Mark this analysis as being processed
    processed_events.add(analysis_cache_key)
    
    try:
        # Extract creator information from Jira
        creator_info = extract_creator_info(ticket)
        if not creator_info:
            print("Could not extract creator information from ticket")
            return
        
        # Analyze ticket for missing information
        parsed_data = parse_jira_ticket(ticket)
        missing_info_analysis = analyze_missing_information(parsed_data, ticket)
        
        # Find creator in Slack
        slack_user_id = find_slack_user_by_email(creator_info.get('email'))
        
        # Invite creator to the incident channel if found in Slack
        if slack_user_id:
            print(f"Inviting ticket creator {slack_user_id} to incident channel")
            invite_user_to_channel(slack_user_id, channel_id)
        
        # Generate and send combined analysis + outreach message
        combined_message = generate_combined_incident_message(
            creator_info, 
            missing_info_analysis, 
            issue_key,
            slack_user_id,
            parsed_data
        )
        
        post_creator_outreach_message(channel_id, combined_message, slack_user_id)
        
        print(f"Successfully completed analysis and outreach for {issue_key}")
        
    except Exception as e:
        # Remove from cache if processing failed so it can be retried
        processed_events.discard(analysis_cache_key)
        print(f"Error in analyze_and_reach_out_to_creator: {e}")
        raise

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

def extract_hospital_name(ticket):
    """Extract hospital name from Jira ticket"""
    try:
        fields = ticket.get("fields", {})
        
        # Debug: Print available fields that might be hospital related
        print(f"DEBUG: All available fields in ticket: {list(fields.keys())}")
        
        # Look for potential hospital fields
        hospital_related_fields = [key for key in fields.keys() if 'customfield' in key]
        print(f"DEBUG: Custom fields in ticket: {hospital_related_fields}")
        
        hospital_field = fields.get(JIRA_HOSPITAL_FIELD)
        
        # Debug: Print the full field structure
        print(f"DEBUG: Looking for hospital field {JIRA_HOSPITAL_FIELD}")
        print(f"DEBUG: Raw hospital field value: {hospital_field}")
        print(f"DEBUG: Type of hospital field: {type(hospital_field)}")
        
        # Handle different field formats
        if isinstance(hospital_field, dict):
            # For select fields or complex objects, try different possible keys
            possible_keys = ["displayName", "value", "name", "key", "id"]
            hospital_name = ""
            
            for key in possible_keys:
                if key in hospital_field and hospital_field[key]:
                    hospital_name = str(hospital_field[key])
                    print(f"DEBUG: Found hospital name '{hospital_name}' using key '{key}'")
                    break
            
            if not hospital_name:
                print(f"DEBUG: No value found in dict keys: {list(hospital_field.keys())}")
                
        elif isinstance(hospital_field, str):
            # For simple text fields
            hospital_name = hospital_field
            print(f"DEBUG: Using string value: '{hospital_name}'")
        elif isinstance(hospital_field, list) and len(hospital_field) > 0:
            # For multi-select fields, take the first one
            first_item = hospital_field[0]
            if isinstance(first_item, dict):
                hospital_name = first_item.get("displayName") or first_item.get("value") or first_item.get("name", "")
            else:
                hospital_name = str(first_item)
            print(f"DEBUG: Using first item from list: '{hospital_name}'")
        else:
            hospital_name = ""
            print(f"DEBUG: Field is empty or unrecognized type")
            
        result = hospital_name.strip() if hospital_name else "unknown"
        print(f"Final extracted hospital name: '{result}' from field {JIRA_HOSPITAL_FIELD}")
        return result
        
    except Exception as e:
        print(f"Error extracting hospital name: {e}")
        return "unknown"

def format_hospital_for_channel(hospital_name):
    """Format hospital name for Slack channel naming"""
    if not hospital_name or hospital_name == "unknown":
        return "unknown"
    
    # Convert to lowercase and replace spaces and special characters
    formatted = hospital_name.lower()
    
    # Replace spaces and common punctuation with hyphens
    formatted = re.sub(r'[\s&.,()\'"/\\]+', '-', formatted)
    
    # Remove any characters that aren't alphanumeric or hyphens
    formatted = re.sub(r'[^a-z0-9-]', '', formatted)
    
    # Remove multiple consecutive hyphens
    formatted = re.sub(r'-+', '-', formatted)
    
    # Remove leading/trailing hyphens
    formatted = formatted.strip('-')
    
    # Limit length to keep channel name reasonable (Slack has 80 char limit total)
    if len(formatted) > 20:
        formatted = formatted[:20].rstrip('-')
    
    # Ensure it's not empty
    if not formatted:
        formatted = "unknown"
    
    print(f"Formatted hospital name '{hospital_name}' to '{formatted}'")
    return formatted

def analyze_missing_information(parsed_data, full_ticket):
    """Use Gemini to analyze what information might be missing from the incident"""
    try:
        # Get additional fields that might be relevant
        fields = full_ticket.get("fields", {})
        priority = fields.get("priority", {}).get("name", "Unknown")
        status = fields.get("status", {}).get("name", "Unknown")
        created = fields.get("created", "Unknown")
        
        # Create a comprehensive analysis prompt
        prompt = f"""You are a supportive incident response assistant working with a fun veterinary software company. Analyze this Jira incident ticket to help identify what additional information might be helpful for faster resolution.

TICKET DETAILS:
Priority: {priority}
Status: {status}
Created: {created}

Summary: {parsed_data['summary']}

Description: {parsed_data['description']}

Please provide a supportive analysis that identifies:
1. What additional information might help our development team resolve this more efficiently?
2. What context could be valuable to have on hand?
3. Are there any helpful details that could speed up the resolution process?

Focus on practical information that would be great to have, such as:
- Steps to reproduce the issue
- Any error messages or logs
- Environment context (production/staging/development)
- Impact scope (how many users or clinics affected)
- Recent changes or deployments that might be related
- Screenshots or examples if available
- Any urgency context

Write this in a collaborative, supportive tone that recognizes the reporter did great work filing the ticket and we're just looking to gather additional context that might help. Keep it friendly and professional - we're all on the same team working to help veterinary practices!"""

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

def generate_combined_incident_message(creator_info, missing_info_analysis, issue_key, slack_user_id, parsed_data):
    """Generate a combined incident analysis and creator outreach message"""
    try:
        creator_name = creator_info.get("display_name", "").split()[0] if creator_info.get("display_name") else "there"
        user_mention = f"<@{slack_user_id}>" if slack_user_id else creator_name
        
        # Get a short summary of the incident
        incident_summary = parsed_data.get('summary', '')[:200] + ('...' if len(parsed_data.get('summary', '')) > 200 else '')
        
        prompt = f"""You are a helpful incident response bot for a fun veterinary software company. Generate a single, comprehensive message that combines incident analysis with creator outreach for ticket {issue_key}.

INCIDENT SUMMARY: {incident_summary}

CREATOR INFO:
Name: {creator_name}

MISSING INFORMATION ANALYSIS:
{missing_info_analysis}

Create a unified message that:
1. Briefly summarizes the incident in 1-2 sentences
2. Thanks the creator for their great work reporting it
3. Mentions that a developer is on the way
4. Asks for specific missing information (based on the analysis) in a friendly, collaborative way
5. Keeps it concise but comprehensive - useful for both the creator and other responders
6. Maintains an encouraging, supportive tone (incidents can be stressful)
7. Emphasizes we're all working together to help veterinary practices

Format it as a single, well-organized message. Use bullet points for the information requests to keep it scannable. Keep it conversational but professional.

The message should be suitable for posting in a Slack channel. Don't include the person's name at the beginning since it will be mentioned separately. Don't include channel mentions or formatting beyond basic Slack markdown."""

        fallback_models = ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-pro"]
        models_to_try = [GEMINI_MODEL] + [m for m in fallback_models if m != GEMINI_MODEL]
        
        for model_name in models_to_try:
            try:
                print(f"Generating combined incident message with model: {model_name}")
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(prompt)
                
                if hasattr(response, 'text') and response.text:
                    message = response.text.strip()
                    # Add user mention at the beginning
                    final_message = f"{user_mention} {message}"
                    print(f"Successfully generated combined incident message with model: {model_name}")
                    return final_message
                elif response.parts:
                    message_text = ''.join(part.text for part in response.parts if hasattr(part, 'text'))
                    if message_text:
                        final_message = f"{user_mention} {message_text.strip()}"
                        print(f"Successfully generated combined incident message with model: {model_name}")
                        return final_message
                
            except Exception as e:
                print(f"Error with model {model_name}: {e}")
                continue
        
        # Fallback message if AI fails
        return f"{user_mention} **Incident Summary:** {incident_summary}\n\nThanks for reporting incident {issue_key}! You did great work getting this submitted. A developer is on the way to help. To help us resolve this faster, please share any additional details like reproduction steps, error messages, environment info, or screenshots. We're working to get this resolved as quickly as possible!"
        
    except Exception as e:
        print(f"Error generating combined incident message: {e}")
        return f"Thanks for reporting incident {issue_key}! You did great work getting this submitted. A developer is on the way to help. Please share any additional details that might help us resolve this faster."

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

def fetch_jira_attachments(issue_key):
    """Fetches media attachments from a Jira ticket."""
    try:
        url = f"https://{JIRA_DOMAIN}/rest/api/3/issue/{issue_key}"
        print(f"Fetching Jira ticket with attachments: {url}")
        
        response = requests.get(
            url,
            auth=(JIRA_USERNAME, JIRA_API_TOKEN),
            headers={"Accept": "application/json"},
            params={"expand": "attachment"}
        )
        
        if response.status_code != 200:
            print(f"Failed to fetch Jira attachments: {response.status_code} - {response.text}")
            return []
        
        ticket = response.json()
        attachments = ticket.get("fields", {}).get("attachment", [])
        
        # Filter for media files (images and videos)
        media_attachments = []
        for attachment in attachments:
            mime_type = attachment.get("mimeType", "")
            filename = attachment.get("filename", "")
            
            if mime_type.startswith(("image/", "video/")):
                media_info = {
                    "id": attachment.get("id"),
                    "filename": filename,
                    "mimeType": mime_type,
                    "size": attachment.get("size", 0),
                    "content": attachment.get("content"),  # Download URL
                    "created": attachment.get("created"),
                    "author": attachment.get("author", {}).get("displayName", "Unknown")
                }
                media_attachments.append(media_info)
                print(f"Found media attachment: {filename} ({mime_type}, {media_info['size']} bytes)")
        
        print(f"Found {len(media_attachments)} media attachments for {issue_key}")
        return media_attachments
        
    except Exception as e:
        print(f"Error fetching Jira attachments for {issue_key}: {e}")
        return []

def download_and_process_media(attachments):
    """Downloads and validates media files from Jira attachments."""
    processed_files = []
    
    # Slack file size limits (1GB max, but we'll be conservative)
    MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB limit
    
    for attachment in attachments:
        try:
            filename = attachment["filename"]
            file_size = attachment["size"]
            mime_type = attachment["mimeType"]
            download_url = attachment["content"]
            
            # Check file size before downloading
            if file_size > MAX_FILE_SIZE:
                print(f"Skipping {filename}: file too large ({file_size} bytes)")
                continue
            
            print(f"Downloading {filename} ({file_size} bytes)")
            
            # Download the file
            download_response = requests.get(
                download_url,
                auth=(JIRA_USERNAME, JIRA_API_TOKEN),
                stream=True  # Stream large files
            )
            
            if download_response.status_code != 200:
                print(f"Failed to download {filename}: {download_response.status_code}")
                continue
            
            # Read file content
            file_content = download_response.content
            
            # Basic validation for images
            if mime_type.startswith("image/") and Image:
                try:
                    # Validate image by opening it
                    img = Image.open(BytesIO(file_content))
                    img.verify()  # Verify it's a valid image
                    print(f"Validated image: {filename} ({img.size[0]}x{img.size[1]})")
                except Exception as e:
                    print(f"Invalid image {filename}: {e}")
                    continue
            
            # Store processed file info
            processed_file = {
                "filename": filename,
                "content": file_content,
                "mime_type": mime_type,
                "size": len(file_content),
                "author": attachment["author"],
                "created": attachment["created"]
            }
            
            processed_files.append(processed_file)
            print(f"Successfully processed: {filename}")
            
        except Exception as e:
            print(f"Error processing attachment {attachment.get('filename', 'unknown')}: {e}")
            continue
    
    print(f"Successfully processed {len(processed_files)} media files")
    return processed_files

def upload_media_to_slack(media_files, channel_id, issue_key):
    """Uploads media files to a Slack channel using the new 2-step upload process."""
    if not media_files:
        print("No media files to upload")
        return []
    
    uploaded_files = []
    
    for media_file in media_files:
        try:
            filename = media_file["filename"]
            content = media_file["content"]
            mime_type = media_file["mime_type"]
            author = media_file["author"]
            created = media_file["created"]
            file_size = len(content)
            
            print(f"Uploading {filename} to Slack channel {channel_id} using new upload method")
            
            # Step 1: Get upload URL
            print(f"Step 1: Getting upload URL for {filename}")
            upload_url_response = requests.get(
                "https://slack.com/api/files.getUploadURLExternal",
                headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                params={
                    "filename": filename,
                    "length": file_size
                }
            )
            
            upload_url_result = upload_url_response.json()
            
            if not upload_url_result.get("ok"):
                error = upload_url_result.get("error", "unknown error")
                print(f"Failed to get upload URL for {filename}: {error}")
                continue
            
            upload_url = upload_url_result.get("upload_url")
            file_id = upload_url_result.get("file_id")
            
            print(f"Got upload URL and file ID {file_id} for {filename}")
            
            # Step 2: Upload file to the URL
            print(f"Step 2: Uploading file content for {filename}")
            upload_response = requests.post(
                upload_url,
                files={"file": (filename, content, mime_type)}
            )
            
            if upload_response.status_code != 200:
                print(f"Failed to upload file content for {filename}: HTTP {upload_response.status_code}")
                continue
            
            print(f"Successfully uploaded file content for {filename}")
            
            # Step 3: Complete the upload and share to channel
            print(f"Step 3: Completing upload and sharing {filename}")
            initial_comment = f"📎 **{filename}** (uploaded by {author} on {created[:10]})\nFrom Jira ticket {issue_key}"
            
            complete_response = requests.post(
                "https://slack.com/api/files.completeUploadExternal",
                headers={
                    "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                    "Content-Type": "application/json"
                },
                json={
                    "files": [{"id": file_id, "title": f"Attachment from {issue_key}"}],
                    "channel_id": channel_id,
                    "initial_comment": initial_comment
                }
            )
            
            complete_result = complete_response.json()
            
            if complete_result.get("ok"):
                uploaded_files.append({
                    "filename": filename,
                    "slack_file_id": file_id,
                    "size": file_size
                })
                print(f"Successfully completed upload for {filename}")
            else:
                error = complete_result.get("error", "unknown error")
                print(f"Failed to complete upload for {filename}: {error}")
                
        except Exception as e:
            print(f"Error uploading {media_file.get('filename', 'unknown')}: {e}")
            continue
    
    print(f"Successfully uploaded {len(uploaded_files)} files to Slack")
    return uploaded_files

def post_media_summary(channel_id, uploaded_files, issue_key):
    """Posts a summary message about uploaded media files."""
    if not uploaded_files:
        return
    
    try:
        file_count = len(uploaded_files)
        total_size = sum(f["size"] for f in uploaded_files)
        size_mb = total_size / (1024 * 1024)
        
        if file_count == 1:
            summary_text = f"📸 Uploaded 1 media file from {issue_key} ({size_mb:.1f} MB)"
        else:
            summary_text = f"📸 Uploaded {file_count} media files from {issue_key} ({size_mb:.1f} MB total)"
        
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers=SLACK_HEADERS,
            json={
                "channel": channel_id,
                "text": summary_text,
                "unfurl_links": False,
                "unfurl_media": False
            }
        ).json()
        
        if response.get("ok"):
            print("Successfully posted media summary message")
        else:
            print(f"Error posting media summary: {response.get('error')}")
            
    except Exception as e:
        print(f"Error posting media summary: {e}")

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
        # Create pattern to match channels for this incident (with any hospital name)
        incident_pattern = f"incident-{issue_key.lower()}-{date_str}-"
        
        response = requests.get(
            "https://slack.com/api/conversations.list",
            headers=SLACK_HEADERS,
            params={"exclude_archived": "false", "limit": 1000}
        ).json()

        if not response.get("ok"):
            print(f"Warning: Could not check existing channels: {response}")
            return False

        existing_channels = {c["name"]: c for c in response.get("channels", [])}
        
        # Check if any channel matching this incident pattern exists and is active
        for channel_name, channel in existing_channels.items():
            if channel_name.startswith(incident_pattern):
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
