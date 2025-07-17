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

# DynamoDB imports for distributed locking
try:
    import boto3
    from botocore.exceptions import ClientError
    DYNAMODB_AVAILABLE = True
except ImportError:
    print("Warning: boto3 not available, will use fallback coordination")
    DYNAMODB_AVAILABLE = False

# --- ENVIRONMENT VARIABLES ---
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
# Legacy Jira credentials (for backward compatibility)
JIRA_USERNAME = os.environ.get("JIRA_USERNAME")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN")
# Firebot Jira credentials (preferred)
FIREBOT_JIRA_USERNAME = os.environ.get("FIREBOT_JIRA_USERNAME", JIRA_USERNAME)
FIREBOT_JIRA_API_TOKEN = os.environ.get("FIREBOT_JIRA_API_TOKEN", JIRA_API_TOKEN)
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

# DynamoDB configuration
DYNAMODB_TABLE_NAME = os.environ.get("DYNAMODB_TABLE_NAME", "firebot-coordination")
DYNAMODB_REGION = os.environ.get("AWS_REGION", "us-east-2")

# Initialize DynamoDB client
if DYNAMODB_AVAILABLE:
    dynamodb = boto3.resource('dynamodb', region_name=DYNAMODB_REGION)
    coordination_table = dynamodb.Table(DYNAMODB_TABLE_NAME)
else:
    coordination_table = None

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
# - groups:history        (read channel history for firebot summary command)
# - channels:history      (read channel history for firebot summary command)

# --- INVESTIGATION CHECKLIST ---
# The bot analyzes each ticket against these 7 critical investigation items:
# 1. Issue replication in customer's application
# 2. Issue replication on Demo instance  
# 3. Steps to reproduce
# 4. Screenshots provided
# 5. Problem start time
# 6. Practice-wide impact
# 7. Multi-practice impact

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

# --- DYNAMODB COORDINATION FUNCTIONS ---
def acquire_incident_lock(issue_key, timeout_minutes=10):
    """Acquire a distributed lock for incident processing using DynamoDB"""
    print(f"DEBUG: DYNAMODB_AVAILABLE = {DYNAMODB_AVAILABLE}")
    print(f"DEBUG: coordination_table = {coordination_table}")
    if not DYNAMODB_AVAILABLE or not coordination_table:
        print("DynamoDB not available, using fallback coordination")
        return True
    
    # Check if table exists
    try:
        print("Attempting to access DynamoDB table...")
        table_status = coordination_table.table_status
        print(f"DynamoDB table exists and is accessible. Status: {table_status}")
    except Exception as e:
        print(f"DynamoDB table not accessible: {e}")
        print("Error type:", type(e).__name__)
        print("Falling back to existing coordination logic")
        return True
    
    try:
        # Calculate expiration time
        now = datetime.datetime.now()
        expiration_time = now + datetime.timedelta(minutes=timeout_minutes)
        expiration_timestamp = int(expiration_time.timestamp())
        
        print("Attempting DynamoDB conditional write for lock acquisition...")
        # Try to acquire lock with conditional write
        response = coordination_table.put_item(
            Item={
                'incident_key': issue_key,
                'lock_acquired_at': now.isoformat(),
                'expiration_time': expiration_timestamp,
                'lambda_instance': os.environ.get('AWS_LAMBDA_REQUEST_ID', 'unknown'),
                'status': 'processing'
            },
            ConditionExpression='attribute_not_exists(incident_key) OR expiration_time < :current_time',
            ExpressionAttributeValues={
                ':current_time': int(now.timestamp())
            }
        )
        print("DynamoDB put_item successful")
        
        print(f"Successfully acquired DynamoDB lock for {issue_key}")
        return True
        
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            print(f"Failed to acquire DynamoDB lock for {issue_key} - another instance is processing")
            return False
        elif e.response['Error']['Code'] == 'ResourceNotFoundException':
            print(f"DynamoDB table not found: {e}")
            print("Falling back to existing coordination logic")
            return True  # Proceed with fallback
        else:
            print(f"DynamoDB error: {e}")
            return True  # Proceed if DynamoDB fails
    
    except Exception as e:
        print(f"Error acquiring DynamoDB lock: {e}")
        return True  # Proceed if lock acquisition fails

def check_event_processed(event_id):
    """Check if an event has been processed using DynamoDB for persistent deduplication"""
    if not DYNAMODB_AVAILABLE or not coordination_table:
        return False
    
    try:
        response = coordination_table.get_item(
            Key={
                'incident_key': f"event-{event_id}"
            }
        )
        
        if 'Item' in response:
            item = response['Item']
            expiration_time = item.get('expiration_time', 0)
            current_time = int(datetime.datetime.now().timestamp())
            
            # Check if event processing record is still valid (24 hours)
            if expiration_time > current_time:
                print(f"Event {event_id} already processed (expires at {expiration_time})")
                return True
            else:
                print(f"Event {event_id} processing record expired, can reprocess")
                return False
        else:
            print(f"Event {event_id} not found in DynamoDB, can process")
            return False
            
    except Exception as e:
        print(f"Error checking event processing status: {e}")
        return False

def mark_event_processed(event_id):
    """Mark an event as processed in DynamoDB for persistent deduplication"""
    if not DYNAMODB_AVAILABLE or not coordination_table:
        return
    
    try:
        # Calculate expiration time (24 hours from now)
        now = datetime.datetime.now()
        expiration_time = now + datetime.timedelta(hours=24)
        expiration_timestamp = int(expiration_time.timestamp())
        
        coordination_table.put_item(
            Item={
                'incident_key': f"event-{event_id}",
                'processed_at': now.isoformat(),
                'expiration_time': expiration_timestamp,
                'lambda_instance': os.environ.get('AWS_LAMBDA_REQUEST_ID', 'unknown'),
                'status': 'processed'
            }
        )
        print(f"Marked event {event_id} as processed in DynamoDB")
        
    except Exception as e:
        print(f"Error marking event as processed: {e}")

def release_incident_lock(issue_key):
    """Release the distributed lock for incident processing"""
    if not DYNAMODB_AVAILABLE or not coordination_table:
        return
    
    try:
        # Delete the lock item
        coordination_table.delete_item(
            Key={
                'incident_key': issue_key
            }
        )
        print(f"Released DynamoDB lock for {issue_key}")
        
    except Exception as e:
        print(f"Error releasing DynamoDB lock: {e}")

def check_incident_processing_status(issue_key):
    """Check if an incident is currently being processed"""
    if not DYNAMODB_AVAILABLE or not coordination_table:
        return False
    
    try:
        response = coordination_table.get_item(
            Key={
                'incident_key': issue_key
            }
        )
        
        if 'Item' in response:
            item = response['Item']
            expiration_time = item.get('expiration_time', 0)
            current_time = int(datetime.datetime.now().timestamp())
            
            # Check if lock is still valid
            if expiration_time > current_time:
                print(f"Incident {issue_key} is currently being processed (expires at {expiration_time})")
                return True
            else:
                print(f"Incident {issue_key} lock has expired, can proceed")
                return False
        else:
            print(f"No lock found for incident {issue_key}")
            return False
            
    except Exception as e:
        print(f"Error checking incident status: {e}")
        return False

def mark_incident_completed(issue_key):
    """Mark an incident as completed in DynamoDB"""
    if not DYNAMODB_AVAILABLE or not coordination_table:
        return
    
    try:
        # Update the status to completed
        response = coordination_table.update_item(
            Key={
                'incident_key': issue_key
            },
            UpdateExpression='SET #status = :status, completed_at = :completed_at',
            ExpressionAttributeNames={
                '#status': 'status'
            },
            ExpressionAttributeValues={
                ':status': 'completed',
                ':completed_at': datetime.datetime.now().isoformat()
            }
        )
        print(f"Marked incident {issue_key} as completed in DynamoDB")
        
    except Exception as e:
        print(f"Error marking incident as completed: {e}")

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
        
        # Check for Slack retry headers
        headers = event.get("headers", {})
        retry_num = headers.get("x-slack-retry-num", "0")
        retry_reason = headers.get("x-slack-retry-reason", "")
        
        if retry_num != "0":
            print(f"⚠️ Processing Slack retry event - Retry #{retry_num}, Reason: {retry_reason}")
        
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
                
                # First check in-memory cache (fast)
                if event_id in processed_events:
                    print(f"❌ Duplicate event detected in cache, skipping: {event_id}")
                    return {"statusCode": 200, "body": "Duplicate event skipped"}
                
                # Then check DynamoDB for persistent deduplication
                if check_event_processed(event_id):
                    print(f"❌ Duplicate event detected in DynamoDB, skipping: {event_id}")
                    # Add to cache to prevent future checks
                    add_to_cache(event_id)
                    return {"statusCode": 200, "body": "Duplicate event skipped"}
                
                # Mark event as processed in DynamoDB immediately
                mark_event_processed(event_id)
                
                # Mark event as processed in memory cache
                print(f"✅ New event detected: {event_id}")
                add_to_cache(event_id)
                
                user_id = event_data.get("user")
                
                # Quick response to Slack to prevent webhook timeout/retry
                try:
                    # Check if this is our bot's response message
                    if is_our_command_response(event_data):
                        print("Skipping our bot's response message to prevent duplicate processing")
                        return {"statusCode": 200, "body": "Bot response skipped"}
                    
                    # Check if this is a firebot command in an incident channel
                    if is_firebot_command(event_data):
                        process_firebot_command(event_data, user_id)
                    else:
                        process_fire_ticket(body, user_id)
                except Exception as err:
                    print("Error during processing:", err)
                    # Remove from processed events if processing failed
                    processed_events.discard(event_id)
                    print(f"Removed failed event from cache: {event_id}")
                    # Still return 200 to prevent Slack retry
                    return {"statusCode": 200, "body": "Processing failed but acknowledged"}
                
                return {"statusCode": 200, "body": "OK"}

        return {"statusCode": 400, "body": "Bad request"}

    except Exception as e:
        print("Unhandled exception in lambda_handler:", e)
        # Return 200 even on exceptions to prevent Slack retries
        return {"statusCode": 200, "body": "Error acknowledged"}

def create_event_id(event_data):
    """Create a unique identifier for deduplication"""
    # Use channel, user, timestamp, and Jira issue key for deduplication
    channel = event_data.get("channel", "")
    user = event_data.get("user", "")
    text = event_data.get("text", "")
    timestamp = event_data.get("ts", "")  # Slack event timestamp
    bot_id = event_data.get("bot_id", "")
    app_id = event_data.get("app_id", "")
    subtype = event_data.get("subtype", "")
    
    # Extract Jira issue key from text for more targeted deduplication
    issue_match = re.search(r"(ISD-\d{5})", text)
    issue_key = issue_match.group(1) if issue_match else ""
    
    # Create more specific identifier that distinguishes user messages from bot messages
    is_bot = bool(bot_id or app_id)
    message_type = "bot" if is_bot else "user"
    
    # Include subtype to distinguish channel_join events
    if subtype:
        message_type = f"{message_type}_{subtype}"
    
    # Create identifier based on what really matters: user + channel + issue + timestamp + message type
    # Also include the Slack event_id if available for better deduplication
    event_id_from_slack = event_data.get("event_id", "")
    unique_string = f"{channel}_{user}_{issue_key}_{timestamp}_{message_type}_{event_id_from_slack}"
    event_id = hashlib.md5(unique_string.encode()).hexdigest()[:16]
    
    # Log for debugging with more detail
    print(f"Event deduplication - Channel: {channel}, User: {user}, Issue: {issue_key}, Timestamp: {timestamp}")
    print(f"Message type: {message_type}, Bot ID: {bot_id}, App ID: {app_id}, Subtype: {subtype}")
    print(f"Slack event_id: {event_id_from_slack}")
    print(f"Generated event ID: {event_id}")
    print(f"Text preview: {text[:100]}..." if len(text) > 100 else f"Text: {text}")
    print(f"Current cache size: {len(processed_events)}")
    
    return event_id

def is_firebot_command(event_data):
    """Check if the message is a firebot command in an incident channel"""
    try:
        text = event_data.get("text", "").strip().lower()
        channel_id = event_data.get("channel", "")
        
        # Check if message starts with "firebot"
        if not text.startswith("firebot"):
            return False
        
        # Check if we're in an incident channel
        if not is_incident_channel(channel_id):
            return False
        
        print(f"Detected firebot command: {text}")
        return True
        
    except Exception as e:
        print(f"Error checking firebot command: {e}")
        return False

def is_incident_channel(channel_id):
    """Check if the channel is an incident channel"""
    try:
        response = requests.get(
            "https://slack.com/api/conversations.info",
            headers=SLACK_HEADERS,
            params={"channel": channel_id}
        ).json()
        
        if not response.get("ok"):
            print(f"Could not get channel info: {response.get('error')}")
            return False
        
        channel_name = response.get("channel", {}).get("name", "")
        return channel_name.startswith("incident-")
        
    except Exception as e:
        print(f"Error checking if incident channel: {e}")
        return False

def process_firebot_command(event_data, user_id):
    """Process firebot commands in incident channels"""
    try:
        text = event_data.get("text", "").strip().lower()
        channel_id = event_data.get("channel", "")
        event_ts = event_data.get("ts", "")
        slack_event_id = event_data.get("event_id", "")
        
        # Skip messages from bots to prevent processing our own messages
        if event_data.get("bot_id") or event_data.get("app_id"):
            print("Skipping bot message to prevent duplicate processing")
            return
        
        # Additional check: skip if the message is from our specific bot user
        bot_user_ids = [os.environ.get("SLACK_BOT_USER_ID"), "U09584DT15X"]
        if user_id in [uid for uid in bot_user_ids if uid]:
            print(f"Skipping message from bot user {user_id} to prevent duplicate processing")
            return
        
        # Create a more specific lock key that includes user, timestamp, and event ID
        command_hash = hashlib.md5(text.encode()).hexdigest()[:8]
        lock_key = f"firebot-cmd-{channel_id}-{user_id}-{command_hash}-{event_ts}"
        if slack_event_id:
            lock_key += f"-{slack_event_id[:8]}"
        
        print(f"Attempting to acquire DynamoDB lock for firebot command: {text}")
        print(f"Lock key: {lock_key}")
        print(f"Current cache contents: {list(processed_events)}")
        
        # Try to acquire DynamoDB lock for this command with shorter timeout
        if not acquire_incident_lock(lock_key, timeout_minutes=1):
            print(f"Failed to acquire lock for firebot command: {text}")
            return
        
        print(f"Successfully acquired lock for firebot command: {text}")
        
        # Create a unique cache key for this firebot command to prevent duplicates
        command_cache_key = f"firebot_{channel_id}_{text}_{user_id}_{event_ts}"
        if command_cache_key in processed_events:
            print(f"Firebot command already processed: {text}")
            release_incident_lock(lock_key)
            return
        
        # Mark command as processed
        processed_events.add(command_cache_key)
        
        # Parse the command
        parts = text.split()
        if len(parts) < 2:
            print("Invalid firebot command - missing subcommand")
            release_incident_lock(lock_key)
            return
        
        command = parts[1]
        
        if command == "summary":
            response = handle_firebot_summary(channel_id, user_id)
            if response:
                track_command_response(channel_id, user_id, text, response)
        elif command == "time":
            response = handle_firebot_time(channel_id, user_id)
            if response:
                track_command_response(channel_id, user_id, text, response)
        elif command == "timeline":
            response = handle_firebot_timeline(channel_id, user_id)
            if response:
                track_command_response(channel_id, user_id, text, response)
        elif command == "resolve":
            response = handle_firebot_resolve(channel_id, user_id)
            if response:
                track_command_response(channel_id, user_id, text, response)
        else:
            print(f"Unknown firebot command: {command}")
            response = post_firebot_help(channel_id)
            if response:
                track_command_response(channel_id, user_id, text, response)
        
        # Release the DynamoDB lock for this command
        release_incident_lock(lock_key)
        print(f"Released lock for firebot command: {text}")
            
    except Exception as e:
        print(f"Error processing firebot command: {e}")
        # Release lock even on error
        try:
            release_incident_lock(lock_key)
        except:
            pass

def handle_firebot_summary(channel_id, user_id):
    """Generate a comprehensive summary of the incident channel"""
    try:
        print(f"Generating incident summary for channel {channel_id}")
        
        # Get channel history
        messages = get_channel_history(channel_id)
        if not messages:
            response_ts = post_message(channel_id, "Could not retrieve channel history for summary.")
            return response_ts
        
        # Generate summary using AI
        summary = generate_incident_summary(messages, channel_id)
        
        # Post the summary
        response_ts = post_message(channel_id, f"📋 **Incident Summary**\n\n{summary}")
        return response_ts
        
    except Exception as e:
        print(f"Error generating incident summary: {e}")
        response_ts = post_message(channel_id, "Sorry, I encountered an error while generating the summary.")
        return response_ts

def handle_firebot_time(channel_id, user_id):
    """Calculate and display how long the incident has been open"""
    try:
        print(f"Calculating incident duration for channel {channel_id}")
        
        # Get channel creation time
        channel_info = get_channel_info(channel_id)
        if not channel_info:
            response_ts = post_message(channel_id, "Could not retrieve channel information.")
            return response_ts
        
        created_timestamp = channel_info.get("created", 0)
        if not created_timestamp:
            response_ts = post_message(channel_id, "Could not determine when this incident started.")
            return response_ts
        
        # Calculate duration
        now = datetime.datetime.now()
        created_time = datetime.datetime.fromtimestamp(created_timestamp)
        duration = now - created_time
        
        # Format duration
        duration_text = format_duration(duration)
        
        # Post the time information
        response_ts = post_message(channel_id, f"⏰ **Incident Duration**\n\nThis incident has been open for: **{duration_text}**\nStarted: {created_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        return response_ts
        
    except Exception as e:
        print(f"Error calculating incident time: {e}")
        response_ts = post_message(channel_id, "Sorry, I encountered an error while calculating the incident duration.")
        return response_ts

def get_channel_history(channel_id, limit=100):
    """Get recent channel history"""
    try:
        response = requests.get(
            "https://slack.com/api/conversations.history",
            headers=SLACK_HEADERS,
            params={
                "channel": channel_id,
                "limit": limit
            }
        ).json()
        
        if not response.get("ok"):
            print(f"Could not get channel history: {response.get('error')}")
            return []
        
        return response.get("messages", [])
        
    except Exception as e:
        print(f"Error getting channel history: {e}")
        return []

def get_channel_info(channel_id):
    """Get channel information including creation time"""
    try:
        response = requests.get(
            "https://slack.com/api/conversations.info",
            headers=SLACK_HEADERS,
            params={"channel": channel_id}
        ).json()
        
        if not response.get("ok"):
            print(f"Could not get channel info: {response.get('error')}")
            return None
        
        return response.get("channel", {})
        
    except Exception as e:
        print(f"Error getting channel info: {e}")
        return None

def generate_incident_summary(messages, channel_id):
    """Generate a comprehensive summary of the incident using AI"""
    try:
        # Format messages for AI analysis with Eastern time
        formatted_messages = []
        eastern_tz = datetime.timezone(datetime.timedelta(hours=-4))  # EDT, adjust for DST as needed
        
        for msg in messages:
            user_id = msg.get("user", "Unknown")
            text = msg.get("text", "")
            timestamp = msg.get("ts", "")
            
            # Look up user info for proper display name
            user_info = get_user_info(user_id)
            display_name = user_info.get("real_name", user_id) if user_info else user_id
            
            if timestamp:
                utc_time = datetime.datetime.fromtimestamp(float(timestamp))
                eastern_time = utc_time.astimezone(eastern_tz)
                time_str = eastern_time.strftime('%I:%M:%S %p EDT')
            else:
                time_str = "Unknown"
            
            formatted_messages.append(f"[{time_str}] {display_name}: {text}")
        
        # Limit to last 50 messages to avoid token limits
        recent_messages = formatted_messages[:50]
        messages_text = "\n".join(recent_messages)
        
        # Generate summary using AI with a more fun prompt
        prompt = f"""You are FireBot 🤖, a fun and helpful incident response assistant! Analyze this Slack conversation and create an engaging summary.

Channel messages:
{messages_text}

Create a fun but professional summary with these sections (use emojis for each section!):

1. 🎬 **Key Events and Timeline:**
   Make it chronological and engaging! Use timestamps in EDT.

2. 👥 **The Dream Team:**
   Who's involved and what are their roles? Make it personal!

3. 📊 **Current Status:**
   Where do we stand? Keep it clear and upbeat!

4. 🎯 **Key Actions Taken:**
   What awesome steps has the team taken?

5. ⏭️ **Next Steps:**
   What's coming up next? Any pending items?

Keep it fun and engaging while maintaining professionalism. Use emojis strategically to highlight key points! Format in markdown with clear sections."""

        fallback_models = ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-pro"]
        models_to_try = [GEMINI_MODEL] + [m for m in fallback_models if m != GEMINI_MODEL]
        
        for model_name in models_to_try:
            try:
                print(f"Generating incident summary with model: {model_name}")
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(prompt)
                
                if hasattr(response, 'text') and response.text:
                    print(f"Successfully generated incident summary with model: {model_name}")
                    return response.text.strip()
                elif response.parts:
                    summary_text = ''.join(part.text for part in response.parts if hasattr(part, 'text'))
                    if summary_text:
                        print(f"Successfully generated incident summary with model: {model_name}")
                        return summary_text.strip()
                
            except Exception as e:
                print(f"Error with model {model_name}: {e}")
                continue
        
        return "Could not generate summary."
        
    except Exception as e:
        print(f"Error generating incident summary: {e}")
        return "Error generating summary."

def format_duration(duration):
    """Format a duration in a human-readable way"""
    total_seconds = int(duration.total_seconds())
    
    if total_seconds < 60:
        return f"{total_seconds} seconds"
    elif total_seconds < 3600:
        minutes = total_seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    elif total_seconds < 86400:
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        return f"{hours} hour{'s' if hours != 1 else ''} and {minutes} minute{'s' if minutes != 1 else ''}"
    else:
        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        return f"{days} day{'s' if days != 1 else ''} and {hours} hour{'s' if hours != 1 else ''}"

def post_firebot_help(channel_id):
    """Post help information for firebot commands"""
    help_text = """🤖 **FireBot Commands** 🤖

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🎯 **Available Commands:**
• `firebot summary` 📋 - Generate a comprehensive summary of the incident
• `firebot time` ⏰ - Show how long the incident has been open
• `firebot timeline` 📊 - Generate a detailed timeline of events and response metrics
• `firebot resolve` ✅ - Mark incident as resolved and post summary to Jira ticket

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📚 **Additional Useful Commands:**
• 👥 `/jsmops all schedules` - View the current on-call schedule

Just type one of these commands in the channel! 🐾"""
    
    response_ts = post_message(channel_id, help_text)
    return response_ts

def post_message(channel_id, text):
    """Post a message to a Slack channel"""
    try:
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers=SLACK_HEADERS,
            json={
                "channel": channel_id,
                "text": text,
                "unfurl_links": False,
                "unfurl_media": False
            }
        ).json()
        
        if not response.get("ok"):
            print(f"Error posting message: {response.get('error')}")
            return None
        
        # Return the timestamp of the posted message
        return response.get("ts")
            
    except Exception as e:
        print(f"Error posting message: {e}")
        return None

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
    
    try:
        print(f"DEBUG: Starting DynamoDB coordination for {issue_key}")
        # Step 0: Acquire distributed lock using DynamoDB
        if not acquire_incident_lock(issue_key):
            print(f"Failed to acquire lock for {issue_key}, another instance is processing")
            return
        
        print(f"Successfully acquired lock for {issue_key}")
        
        # Step 1: Check if incident already processed by looking for existing channels
        if check_incident_already_processed(issue_key):
            print(f"Incident {issue_key} already processed, skipping")
            release_incident_lock(issue_key)
            return
        
        # Step 2: Fetch Jira data to get hospital name
        jira_data = fetch_jira_data(issue_key)
        if jira_data.status_code != 200:
            release_incident_lock(issue_key)
            raise Exception(f"Failed to fetch Jira ticket data: {jira_data.text}")

        ticket = jira_data.json()
        print(f"Successfully fetched Jira ticket: {issue_key}")
        
        parsed_data = parse_jira_ticket(ticket)
        print(f"Parsed ticket data - Summary length: {len(parsed_data['summary'])}, Description length: {len(parsed_data['description'])}")
        
        # Extract hospital name and format for channel name
        hospital_name = extract_hospital_name(ticket)
        hospital_slug = format_hospital_for_channel(hospital_name)
        
        date_str = datetime.datetime.now().strftime("%Y%m%d")
        channel_slug = issue_key.lower()
        base_channel_name = f"incident-{channel_slug}-{date_str}-{hospital_slug}"
        
        # Step 3: Create the incident channel
        channel_id, channel_name = create_incident_channel_with_coordination(base_channel_name, issue_key)
        if not channel_id:
            print(f"Failed to create channel for {issue_key}")
            release_incident_lock(issue_key)
            return
            
        print(f"Successfully created channel: {channel_name} ({channel_id})")
        
        # Update Jira ticket with Slack channel link
        update_jira_with_slack_link(issue_key, channel_name, channel_id)
        
        # Step 4: Post coordination message to claim ownership
        post_coordination_message(channel_id, issue_key)
        
        # Step 4: Invite user to channel
        invite_user_to_channel(user_id, channel_id)
        
        # Step 5: Post greeting message to incident channel (only once per incident)
        greeting_cache_key = f"greeting_{issue_key}"
        if greeting_cache_key not in processed_events:
            processed_events.add(greeting_cache_key)
            post_incident_channel_greeting(channel_id, issue_key)
            print(f"Posted greeting message for {issue_key}")
        else:
            print(f"Greeting message for {issue_key} already posted, skipping")
        
        # Step 6: Post welcome message to source channel (only once per incident)
        welcome_cache_key = f"welcome_{issue_key}"
        if welcome_cache_key not in processed_events:
            processed_events.add(welcome_cache_key)
            post_welcome_message(event_data["event"]["channel"], channel_name, channel_id)
            print(f"Posted welcome message for {issue_key}")
        else:
            print(f"Welcome message for {issue_key} already posted, skipping")
        
        # Step 7: Generate and post summary (only once per incident)
        summary_cache_key = f"summary_{issue_key}"
        if summary_cache_key not in processed_events:
            processed_events.add(summary_cache_key)
            summary = generate_gemini_summary(parsed_data)
            print(f"Generated summary length: {len(summary)}")
            post_summary_message(channel_id, summary)
            print(f"Posted summary for {issue_key}")
        else:
            print(f"Summary for {issue_key} already posted, skipping")
        
        # Step 8: Fetch attachments once for both analysis and media processing
        print(f"Fetching attachments for analysis and media processing: {issue_key}")
        attachments = fetch_jira_attachments(issue_key)
        
        # Step 9: Analyze ticket for missing information and reach out to creator (critical step)
        analysis_cache_key = f"analysis_{issue_key}"
        if analysis_cache_key not in processed_events:
            print(f"Starting analysis and outreach for {issue_key}")
            try:
                analyze_and_reach_out_to_creator(ticket, channel_id, issue_key, attachments)
                print(f"Successfully completed analysis and outreach for {issue_key}")
            except Exception as e:
                print(f"Error in ticket analysis and outreach for {issue_key}: {e}")
                # Don't fail the entire process, but don't mark as processed either
        else:
            print(f"Analysis for {issue_key} already completed, skipping")
        
        # Step 10: Process media attachments from Jira ticket
        media_cache_key = f"media_{issue_key}"
        if media_cache_key not in processed_events:
            try:
                processed_events.add(media_cache_key)
                
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
            except Exception as e:
                print(f"Error in media processing for {issue_key}: {e}")
                # Don't fail the entire process if media processing fails
                processed_events.discard(media_cache_key)  # Allow retry on next run
        else:
            print(f"Media for {issue_key} already processed, skipping")
        
        print(f"Successfully processed fire ticket for {issue_key}")
        
        # Mark incident as completed and release lock
        mark_incident_completed(issue_key)
        release_incident_lock(issue_key)
        
    except Exception as e:
        print(f"Error processing fire ticket {issue_key}: {e}")
        # Release lock even on error
        release_incident_lock(issue_key)
        raise

def attempt_immediate_coordination(issue_key):
    """Immediate coordination check - post coordination message and check for existing ones"""
    try:
        # First, check if there's already a coordination message in any channel for this incident
        date_str = datetime.datetime.now().strftime("%Y%m%d")
        incident_pattern = f"incident-{issue_key.lower()}-{date_str}-"
        
        response = requests.get(
            "https://slack.com/api/conversations.list",
            headers=SLACK_HEADERS,
            params={"exclude_archived": "false", "limit": 1000}
        ).json()

        if not response.get("ok"):
            print(f"Warning: Could not list channels for immediate coordination: {response}")
            return True  # Proceed if we can't check
            
        existing_channels = {c["name"]: c for c in response.get("channels", [])}
        
        # Check for existing coordination messages in any incident channel
        for channel_name, channel in existing_channels.items():
            if channel_name.startswith(incident_pattern) and not channel.get("is_archived"):
                print(f"Checking existing channel for coordination messages: {channel_name}")
                
                # Look for coordination messages in the last 5 minutes
                five_minutes_ago = datetime.datetime.now() - datetime.timedelta(minutes=5)
                oldest_timestamp = five_minutes_ago.timestamp()
                
                history_response = requests.get(
                    "https://slack.com/api/conversations.history",
                    headers=SLACK_HEADERS,
                    params={
                        "channel": channel["id"],
                        "oldest": oldest_timestamp,
                        "limit": 20
                    }
                ).json()
                
                if history_response.get("ok"):
                    messages = history_response.get("messages", [])
                    bot_user_id = os.environ.get("SLACK_BOT_USER_ID", "U09584DT15X")
                    
                    for message in messages:
                        # Only check messages from our bot
                        if message.get("user") != bot_user_id and message.get("bot_id") != "B09584DRW4R":
                            continue
                            
                        text = message.get("text", "")
                        
                        # If we find a coordination message, another instance is processing
                        if f"🔄 Processing incident {issue_key}" in text:
                            print(f"Found existing coordination message for {issue_key}, aborting")
                            return False
        
        # No existing coordination found, proceed with processing
        print(f"No existing coordination found for {issue_key}, proceeding")
        return True
        
    except Exception as e:
        print(f"Error in immediate coordination: {e}")
        return True  # Proceed if coordination fails

def attempt_incident_coordination(issue_key):
    """Attempt to coordinate incident processing across Lambda instances using Slack"""
    try:
        # Check if there's already an active incident channel for today
        date_str = datetime.datetime.now().strftime("%Y%m%d")
        incident_pattern = f"incident-{issue_key.lower()}-{date_str}-"
        
        response = requests.get(
            "https://slack.com/api/conversations.list",
            headers=SLACK_HEADERS,
            params={"exclude_archived": "false", "limit": 1000}
        ).json()

        if not response.get("ok"):
            print(f"Warning: Could not list channels for coordination: {response}")
            return True  # Proceed if we can't check
            
        existing_channels = {c["name"]: c for c in response.get("channels", [])}
        
        # If any active channel exists for this incident, check if processing already completed
        for channel_name, channel in existing_channels.items():
            if channel_name.startswith(incident_pattern) and not channel.get("is_archived"):
                print(f"Found existing channel for {issue_key}: {channel_name}")
                
                # Check if processing is already completed by looking for bot workflow messages
                if is_workflow_already_completed(channel["id"], issue_key):
                    print(f"Workflow already completed for {issue_key}, aborting duplicate processing")
                    return False
                else:
                    print(f"Proceeding with processing {issue_key} in existing channel")
                    return True
        
        print(f"No existing channels found for {issue_key}, proceeding with processing")
        return True
        
    except Exception as e:
        print(f"Error in incident coordination: {e}")
        return True  # Proceed if coordination fails

def is_workflow_already_completed(channel_id, issue_key):
    """Check if the workflow has been completed by looking for recent bot messages"""
    try:
        # Look for messages in the last 10 minutes
        ten_minutes_ago = datetime.datetime.now() - datetime.timedelta(minutes=10)
        oldest_timestamp = ten_minutes_ago.timestamp()
        
        response = requests.get(
            "https://slack.com/api/conversations.history",
            headers=SLACK_HEADERS,
            params={
                "channel": channel_id,
                "oldest": oldest_timestamp,
                "limit": 50
            }
        ).json()
        
        if not response.get("ok"):
            print(f"Warning: Could not check channel history: {response}")
            return False
        
        messages = response.get("messages", [])
        
        # Look for evidence of completed workflow steps
        found_summary = False
        found_analysis = False
        found_media = False
        found_coordination = False
        
        bot_user_id = os.environ.get("SLACK_BOT_USER_ID", "U09584DT15X")
        
        for message in messages:
            # Only check messages from our bot
            if message.get("user") != bot_user_id and message.get("bot_id") != "B09584DRW4R":
                continue
                
            text = message.get("text", "")
            
            # Check for coordination message (most recent indicator of processing)
            if f"🔄 Processing incident {issue_key}" in text:
                found_coordination = True
                print(f"Found coordination message for {issue_key}")
            
            # Check for workflow completion indicators
            if "*Incident Summary:*" in text:
                found_summary = True
                print(f"Found summary message for {issue_key}")
            
            if ("Thanks for reporting incident" in text or 
                "additional details" in text or 
                "A developer is" in text):
                found_analysis = True
                print(f"Found analysis message for {issue_key}")
            
            if ("Uploaded" in text and "media file" in text and issue_key in text):
                found_media = True
                print(f"Found media upload message for {issue_key}")
        
        # If we found coordination message but no summary/analysis, another instance is processing
        if found_coordination and not found_summary:
            print(f"Found coordination message but no summary - another instance is processing {issue_key}")
            return True  # Abort this instance
        
        # Consider workflow completed if we have summary + analysis
        # (media is optional depending on attachments)
        workflow_completed = found_summary and found_analysis
        
        print(f"Workflow status for {issue_key}: coordination={found_coordination}, summary={found_summary}, analysis={found_analysis}, media={found_media}, completed={workflow_completed}")
        
        return workflow_completed
        
    except Exception as e:
        print(f"Error checking workflow completion: {e}")
        return False

def create_incident_channel_with_coordination(base_name, issue_key):
    """Create incident channel with simplified coordination since we have atomic lock"""
    try:
        original_name = base_name.lower()
        
        # Since we have atomic lock, just create the channel
        print(f"Creating incident channel: {original_name}")
        create_response = requests.post(
            "https://slack.com/api/conversations.create",
            headers=SLACK_HEADERS,
            json={"name": original_name, "is_private": False}
        ).json()
        
        if create_response.get("ok"):
            channel_id = create_response["channel"]["id"]
            print(f"Successfully created incident channel: {original_name}")
            return channel_id, original_name
            
        elif create_response.get("error") == "name_taken":
            print(f"Channel {original_name} already exists, using existing channel")
            
            # Channel exists, get its ID
            response = requests.get(
                "https://slack.com/api/conversations.list",
                headers=SLACK_HEADERS,
                params={"exclude_archived": "false", "limit": 1000}
            ).json()
            
            if response.get("ok"):
                existing_channels = {c["name"]: c for c in response.get("channels", [])}
                if original_name in existing_channels:
                    channel = existing_channels[original_name]
                    if not channel.get("is_archived"):
                        return channel["id"], original_name
            
            # Fall back to numbered channels if needed
            return create_incident_channel(base_name)
        else:
            print(f"Error creating channel: {create_response.get('error')}")
            return create_incident_channel(base_name)
            
    except Exception as e:
        print(f"Error in channel creation: {e}")
        return create_incident_channel(base_name)

def post_coordination_message(channel_id, issue_key):
    """Post a coordination message to claim processing ownership"""
    try:
        coordination_text = f"🔄 Processing incident {issue_key}..."
        
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers=SLACK_HEADERS,
            json={
                "channel": channel_id,
                "text": coordination_text,
                "unfurl_links": False,
                "unfurl_media": False
            }
        ).json()
        
        if response.get("ok"):
            print(f"Posted coordination message for {issue_key}")
        else:
            print(f"Warning: Could not post coordination message: {response.get('error')}")
            
    except Exception as e:
        print(f"Error posting coordination message: {e}")

def analyze_and_reach_out_to_creator(ticket, channel_id, issue_key, attachments):
    """Analyze ticket for missing info and reach out to creator"""
    print(f"Analyzing ticket {issue_key} for missing information...")
    
    try:
        # Extract creator information from Jira
        creator_info = extract_creator_info(ticket)
        if not creator_info:
            print("Could not extract creator information from ticket")
            return
        
        # Analyze ticket for missing information using structured checklist
        parsed_data = parse_jira_ticket(ticket)
        
        # Run the structured checklist analysis (attachments already fetched)
        checklist_results = analyze_incident_checklist(parsed_data, ticket, attachments)
        
        # Find creator in Slack
        slack_user_id = find_slack_user_by_email(creator_info.get('email'))
        
        # Invite creator to the incident channel if found in Slack
        if slack_user_id:
            print(f"Inviting ticket creator {slack_user_id} to incident channel")
            invite_user_to_channel(slack_user_id, channel_id)
        
        # Generate and send combined analysis + outreach message
        combined_message = generate_combined_incident_message(
            creator_info, 
            checklist_results, 
            issue_key,
            slack_user_id,
            parsed_data
        )
        
        post_creator_outreach_message(channel_id, combined_message, slack_user_id)
        
        # Mark analysis as completed in the main cache
        processed_events.add(f"analysis_{issue_key}")
        print(f"Successfully completed analysis and outreach for {issue_key}")
        
    except Exception as e:
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

def analyze_incident_checklist(parsed_data, full_ticket, attachments):
    """Analyze ticket against specific investigation checklist items"""
    try:
        # Get additional fields that might be relevant
        fields = full_ticket.get("fields", {})
        priority = fields.get("priority", {}).get("name", "Unknown")
        status = fields.get("status", {}).get("name", "Unknown")
        created = fields.get("created", "Unknown")
        
        # Create a structured analysis prompt for the 7 specific items
        prompt = f"""You are an incident response assistant for a veterinary software company. Analyze this Jira ticket against our investigation checklist.

TICKET DETAILS:
Priority: {priority}
Status: {status}
Created: {created}
Attachments: {len(attachments)} media files found

Summary: {parsed_data['summary']}

Description: {parsed_data['description']}

Please analyze this ticket and determine if it contains information about each of these 7 critical investigation items. For each item, respond with either "FOUND" or "MISSING" followed by a brief explanation. Pay special attention to information already provided in the description - don't ask for information that's already clearly stated.

INVESTIGATION CHECKLIST:
1. Issue replication in customer's application - Has the reporter confirmed they can reproduce this issue in their own application? Look for statements about current impact.
2. Issue replication on Demo instance - Has anyone tested this on our Demo/staging environment?
3. Steps to reproduce - Are clear, step-by-step reproduction instructions provided?
4. Screenshots provided - Are screenshots or visual evidence included? (We found {len(attachments)} media files)
5. Problem start time - When did this issue first start occurring for the customer? Look for any timing information in the description.
6. Practice-wide impact - Is this affecting the entire practice/all users, or just specific users? Look for statements about scope of impact.
7. Multi-practice impact - Are other veterinary practices experiencing this same issue?

For each item, respond in this exact format:
1. [FOUND/MISSING]: Brief explanation
2. [FOUND/MISSING]: Brief explanation
3. [FOUND/MISSING]: Brief explanation
4. [FOUND/MISSING]: Brief explanation
5. [FOUND/MISSING]: Brief explanation
6. [FOUND/MISSING]: Brief explanation
7. [FOUND/MISSING]: Brief explanation

Be thorough but concise in your analysis. If information is clearly stated in the description, mark it as FOUND and quote the relevant text."""

        fallback_models = ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-pro"]
        models_to_try = [GEMINI_MODEL] + [m for m in fallback_models if m != GEMINI_MODEL]
        
        for model_name in models_to_try:
            try:
                print(f"Analyzing incident checklist with model: {model_name}")
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(prompt)
                
                if hasattr(response, 'text') and response.text:
                    analysis = response.text.strip()
                    print(f"Successfully analyzed incident checklist with model: {model_name}")
                    return parse_checklist_analysis(analysis)
                elif response.parts:
                    analysis_text = ''.join(part.text for part in response.parts if hasattr(part, 'text'))
                    if analysis_text:
                        print(f"Successfully analyzed incident checklist with model: {model_name}")
                        return parse_checklist_analysis(analysis_text.strip())
                
            except Exception as e:
                print(f"Error with model {model_name}: {e}")
                continue
        
        return create_default_checklist_result()
        
    except Exception as e:
        print(f"Error analyzing incident checklist: {e}")
        return create_default_checklist_result()

def parse_checklist_analysis(analysis_text):
    """Parse the AI analysis into structured checklist results"""
    checklist_items = [
        "Issue replication in customer's application",
        "Issue replication on Demo instance", 
        "Steps to reproduce",
        "Screenshots provided",
        "Problem start time",
        "Practice-wide impact",
        "Multi-practice impact"
    ]
    
    results = {
        "missing_items": [],
        "found_items": [],
        "analysis_text": analysis_text
    }
    
    lines = analysis_text.split('\n')
    for i, line in enumerate(lines):
        if line.strip() and any(str(j+1) + '.' in line for j in range(7)):
            if 'MISSING' in line.upper():
                if i < len(checklist_items):
                    results["missing_items"].append({
                        "item": checklist_items[i] if i < len(checklist_items) else f"Item {i+1}",
                        "explanation": line.split(':', 1)[1].strip() if ':' in line else line
                    })
            elif 'FOUND' in line.upper():
                if i < len(checklist_items):
                    results["found_items"].append({
                        "item": checklist_items[i] if i < len(checklist_items) else f"Item {i+1}",
                        "explanation": line.split(':', 1)[1].strip() if ':' in line else line
                    })
    
    print(f"Parsed checklist: {len(results['missing_items'])} missing, {len(results['found_items'])} found")
    return results

def create_default_checklist_result():
    """Create a default checklist result when AI analysis fails"""
    return {
        "missing_items": [],
        "found_items": [],
        "analysis_text": "Could not complete checklist analysis due to technical issues."
    }

def generate_missing_items_requests(missing_items, issue_key, parsed_data):
    """Generate specific requests for missing investigation items"""
    if not missing_items:
        return "Great news! This ticket appears to have all the key investigation details we need. 🎉"
    
    try:
        # Format missing items more efficiently
        missing_items_text = "\n".join([f"• {item['item']}" for item in missing_items])
        
        # More concise prompt
        prompt = f"""Generate a brief, friendly message requesting missing details for {issue_key}.

Context: {parsed_data.get('summary', '')[:100]}

Missing Items:
{missing_items_text}

Format: Thank reporter briefly, then list specific actionable requests for each missing item. Keep tone encouraging but direct. No formal closings."""

        # Set generation config for more efficient responses
        generation_config = {
            "max_output_tokens": 200,  # Limit response length
            "temperature": 0.3,        # More focused/deterministic
            "top_p": 0.8              # More focused token selection
        }
        
        fallback_models = ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-pro"]
        models_to_try = [GEMINI_MODEL] + [m for m in fallback_models if m != GEMINI_MODEL]
        
        for model_name in models_to_try:
            try:
                print(f"Generating missing items requests with model: {model_name}")
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(
                    prompt,
                    generation_config=generation_config
                )
                
                if hasattr(response, 'text') and response.text:
                    print(f"Successfully generated missing items requests with model: {model_name}")
                    return response.text.strip()
                elif response.parts:
                    request_text = ''.join(part.text for part in response.parts if hasattr(part, 'text'))
                    if request_text:
                        print(f"Successfully generated missing items requests with model: {model_name}")
                        return request_text.strip()
                
                print(f"Empty response from model: {model_name}")
                
            except Exception as e:
                print(f"Error with model {model_name}: {e}")
                if model_name == models_to_try[-1]:  # Last model failed
                    return generate_fallback_missing_items_message(missing_items)
                continue
        
        # If all models fail, use fallback
        return generate_fallback_missing_items_message(missing_items)
        
    except Exception as e:
        print(f"Error generating missing items requests: {e}")
        return generate_fallback_missing_items_message(missing_items)

def generate_fallback_missing_items_message(missing_items):
    """Generate a simple fallback message for missing items"""
    items_list = "\n".join([f"• {item['item']}" for item in missing_items])
    
    return f"""Thanks for reporting this issue! To help our development team investigate more efficiently, could you please provide some additional details:

{items_list}"""

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

def generate_combined_incident_message(creator_info, checklist_results, issue_key, slack_user_id, parsed_data):
    """Generate a combined incident analysis and creator outreach message using structured checklist"""
    try:
        # Safely extract creator name
        creator_name = "there"
        if creator_info and creator_info.get("display_name"):
            try:
                creator_name = creator_info.get("display_name", "").split()[0]
            except (AttributeError, IndexError):
                creator_name = "there"
        
        user_mention = f"<@{slack_user_id}>" if slack_user_id else creator_name
        
        # Safely get incident summary and description
        incident_summary = ""
        incident_description = ""
        if parsed_data:
            try:
                summary_text = str(parsed_data.get('summary', ''))
                incident_summary = summary_text[:200] + ('...' if len(summary_text) > 200 else '')
                
                description_text = str(parsed_data.get('description', ''))
                incident_description = description_text[:500] + ('...' if len(description_text) > 500 else '')
            except (TypeError, AttributeError):
                incident_summary = "Issue details available in ticket"
                incident_description = ""
        
        # Safely extract checklist results
        missing_items = []
        found_items = []
        if checklist_results and isinstance(checklist_results, dict):
            missing_items = checklist_results.get('missing_items', []) or []
            found_items = checklist_results.get('found_items', []) or []
        
        # Ensure they are lists
        if not isinstance(missing_items, list):
            missing_items = []
        if not isinstance(found_items, list):
            found_items = []
        
        if not missing_items:
            # All investigation items are present
            return f"{user_mention} **Incident Summary:** {incident_summary}\n\nThanks for reporting incident {issue_key}! 🎉 You did fantastic work providing all the key investigation details we need. A developer is on the way to help resolve this."
        
        # Generate specific requests for missing items
        missing_items_request = generate_missing_items_requests(missing_items, issue_key, parsed_data)
        
        # Safely build found items summary
        found_items_summary = ""
        try:
            if found_items:
                found_items_names = []
                for item in found_items[:3]:  # Only first 3 items
                    if isinstance(item, dict) and 'item' in item:
                        found_items_names.append(str(item['item']))
                
                if found_items_names:
                    found_items_summary = ', '.join(found_items_names)
                    if len(found_items) > 3:
                        found_items_summary += '...'
        except (TypeError, AttributeError, KeyError):
            found_items_summary = "Several items"
        
        prompt = f"""You are a helpful incident response bot for a veterinary software company. Create a supportive message that combines incident acknowledgment with specific investigation requests.

INCIDENT: {issue_key}
SUMMARY: {incident_summary}
DESCRIPTION: {incident_description}
CREATOR: {creator_name}

FOUND ITEMS ({len(found_items)}): {found_items_summary}

MISSING ITEMS REQUEST:
{missing_items_request}

Create a message that:
1. Briefly acknowledges the incident in 1-2 sentences
2. Thanks the creator for their work reporting it
3. Mentions a developer is on the way
4. Includes the specific missing items request
5. Maintains an encouraging, collaborative tone
6. Keeps it concise and well-organized

Don't include the person's name at the beginning since it will be mentioned separately.
Use friendly but professional language.
Base your response on both the summary and description context.
Keep it direct and concise - no formal closings like 'Best Regards' or 'Thanks again'."""

        fallback_models = ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-pro"]
        models_to_try = [GEMINI_MODEL] + [m for m in fallback_models if m != GEMINI_MODEL]
        
        for model_name in models_to_try:
            try:
                print(f"Generating combined incident message with model: {model_name}")
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(prompt)
                
                if hasattr(response, 'text') and response.text:
                    message = response.text.strip()
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
        fallback_message = f"**Incident Summary:** {incident_summary}\n\nThanks for reporting incident {issue_key}! A developer is on the way to help.\n\n{missing_items_request}"
        return f"{user_mention} {fallback_message}"
        
    except Exception as e:
        print(f"Error generating combined incident message: {e}")
        # Ultimate fallback with safe user mention
        safe_mention = f"<@{slack_user_id}>" if slack_user_id else ""
        return f"{safe_mention} Thanks for reporting incident {issue_key}! You did great work getting this submitted. A developer is on the way to help. Please share any additional details that might help us resolve this faster."

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
        auth=(FIREBOT_JIRA_USERNAME, FIREBOT_JIRA_API_TOKEN),
        headers={"Accept": "application/json"}
    )
    print("Jira response status:", response.status_code)
    return response

def parse_jira_ticket(ticket):
    fields = ticket.get("fields", {})
    
    # Get the standard summary field
    summary = fields.get("summary", "")
    
    # Handle Jira description which might be in ADF format
    description_field = fields.get("description", "")
    description = ""
    
    if isinstance(description_field, dict):
        # ADF format - extract text content
        description = extract_text_from_adf(description_field)
    elif isinstance(description_field, str):
        # Plain text
        description = description_field
    
    # Get hospital/practice info
    hospitals = fields.get("customfield_10348", [])
    
    return {
        "summary": summary,
        "description": description,
        "hospitals": hospitals
    }

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
            auth=(FIREBOT_JIRA_USERNAME, FIREBOT_JIRA_API_TOKEN),
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
                auth=(FIREBOT_JIRA_USERNAME, FIREBOT_JIRA_API_TOKEN),
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
            "text": f"""🚨 **INCIDENT CHANNEL CREATED** 🚨

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📢 Please move all communications to <#{new_channel_id}|{new_channel_name}>

I'll help coordinate the incident response there! 🤖"""
        }
    ).json()
    if not response.get("ok"):
        print(f"Error posting welcome message: {response.get('error')}")

def post_summary_message(channel_id, summary):
    """Post a fun and visually appealing summary message"""
    response = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers=SLACK_HEADERS,
        json={
            "channel": channel_id,
            "text": f"""🎯 **Incident Summary** 🎯

━━━━━━━━━━━ 🔍 SUMMARY TIME! 🔍 ━━━━━━━━━━━

{summary}

━━━━━━━━━━━ 🤖 END SUMMARY 🤖 ━━━━━━━━━━━

Need more details? Try:
• `firebot timeline` 📊 - For a detailed event timeline
• `firebot time` ⏰ - To check incident duration
• `firebot resolve` ✅ - When everything's fixed!

Stay awesome! 🌟"""
        }
    ).json()
    if not response.get("ok"):
        print(f"Error posting summary message: {response.get('error')}")

def check_incident_already_processed(issue_key):
    """Check if this incident has already been processed by looking for existing active channel with completed workflow"""
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
                    channel_id = channel["id"]
                    
                    # Check if the workflow has been completed by looking for specific bot messages
                    if is_incident_workflow_completed(channel_id, issue_key):
                        print(f"Incident {issue_key} workflow already completed in channel {channel_name}")
                        return True
                    else:
                        print(f"Channel {channel_name} exists but workflow not completed, allowing processing")
                        return False
        
        print(f"No existing channel found for incident {issue_key}, proceeding with processing")
        return False
        
    except Exception as e:
        print(f"Error checking if incident already processed: {e}")
        return False

def is_incident_workflow_completed(channel_id, issue_key):
    """Check if the full incident workflow has been completed by looking for analysis message"""
    try:
        # Get messages from the last hour to check for workflow completion
        one_hour_ago = datetime.datetime.now() - datetime.timedelta(hours=1)
        oldest_timestamp = one_hour_ago.timestamp()
        
        response = requests.get(
            "https://slack.com/api/conversations.history",
            headers=SLACK_HEADERS,
            params={
                "channel": channel_id,
                "oldest": oldest_timestamp,
                "limit": 50  # Check more messages
            }
        ).json()
        
        if not response.get("ok"):
            print(f"Warning: Could not get channel history for workflow check: {response}")
            return False
        
        messages = response.get("messages", [])
        
        # Look for specific indicators that the full workflow has completed
        workflow_indicators = [
            "Thanks for reporting incident",  # Creator outreach message
            "additional details",            # Analysis request
            "A developer is on the way",     # Acknowledgment message
        ]
        
        completed_steps = 0
        for message in messages:
            message_text = message.get("text", "")
            for indicator in workflow_indicators:
                if indicator in message_text:
                    completed_steps += 1
                    break
        
        # If we found evidence of the analysis/outreach workflow, consider it completed
        workflow_completed = completed_steps >= 1
        print(f"Workflow completion check: found {completed_steps} indicators, completed: {workflow_completed}")
        return workflow_completed
        
    except Exception as e:
        print(f"Error checking workflow completion: {e}")
        return False

def create_atomic_lock_channel(channel_name, issue_key):
    """Creates a temporary channel to prevent duplicate incident processing."""
    try:
        print(f"Attempting to create atomic lock channel: {channel_name}")
        create_response = requests.post(
            "https://slack.com/api/conversations.create",
            headers=SLACK_HEADERS,
            json={"name": channel_name, "is_private": False}
        ).json()

        if create_response.get("ok"):
            print(f"Successfully created atomic lock channel: {channel_name}")
            return True
        elif create_response.get("error") == "name_taken":
            # Lock channel already exists, check if it's recent (within last 5 minutes)
            print(f"Lock channel {channel_name} already exists, checking if it's recent")
            if is_lock_channel_recent(channel_name):
                print(f"Lock channel {channel_name} is recent, another instance is processing")
                return False
            else:
                print(f"Lock channel {channel_name} is old, checking if it's archived")
                if is_channel_archived(channel_name):
                    print(f"Lock channel {channel_name} is archived, using timestamp suffix")
                    import time
                    timestamp = int(time.time())
                    new_channel_name = f"{channel_name}-{timestamp}"
                    
                    timestamp_response = requests.post(
                        "https://slack.com/api/conversations.create",
                        headers=SLACK_HEADERS,
                        json={"name": new_channel_name, "is_private": False}
                    ).json()
                    
                    if timestamp_response.get("ok"):
                        print(f"Successfully created atomic lock channel with timestamp: {new_channel_name}")
                        return True
                    else:
                        print(f"Failed to create atomic lock channel with timestamp: {timestamp_response.get('error')}")
                        return False
                else:
                    print(f"Lock channel {channel_name} is old but not archived, using timestamp suffix")
                    import time
                    timestamp = int(time.time())
                    new_channel_name = f"{channel_name}-{timestamp}"
                    
                    timestamp_response = requests.post(
                        "https://slack.com/api/conversations.create",
                        headers=SLACK_HEADERS,
                        json={"name": new_channel_name, "is_private": False}
                    ).json()
                    
                    if timestamp_response.get("ok"):
                        print(f"Successfully created atomic lock channel with timestamp: {new_channel_name}")
                        return True
                    else:
                        print(f"Failed to create atomic lock channel with timestamp: {timestamp_response.get('error')}")
                        return False
        else:
            print(f"Failed to create atomic lock channel {channel_name}: {create_response.get('error')}")
            return False
    except Exception as e:
        print(f"Error creating atomic lock channel: {e}")
        return False

def is_lock_channel_recent(channel_name):
    """Check if a lock channel was created recently (within last 5 minutes)"""
    try:
        # Get channel info to check creation time
        response = requests.get(
            "https://slack.com/api/conversations.list",
            headers=SLACK_HEADERS,
            params={"exclude_archived": "false", "limit": 1000}
        ).json()
        
        if not response.get("ok"):
            print(f"Could not list channels to check lock age: {response.get('error')}")
            return True  # Assume recent if we can't check
        
        channels = response.get("channels", [])
        for channel in channels:
            if channel.get("name") == channel_name:
                created_timestamp = channel.get("created", 0)
                if created_timestamp:
                    created_time = datetime.datetime.fromtimestamp(created_timestamp)
                    now = datetime.datetime.now()
                    age = now - created_time
                    
                    # Consider recent if less than 5 minutes old
                    is_recent = age.total_seconds() < 300  # 5 minutes
                    print(f"Lock channel {channel_name} age: {age}, is_recent: {is_recent}")
                    return is_recent
        
        print(f"Lock channel {channel_name} not found in channel list")
        return False
        
    except Exception as e:
        print(f"Error checking lock channel age: {e}")
        return True  # Assume recent if we can't check

def is_channel_archived(channel_name):
    """Check if a channel is archived"""
    try:
        response = requests.get(
            "https://slack.com/api/conversations.list",
            headers=SLACK_HEADERS,
            params={"exclude_archived": "false", "limit": 1000}
        ).json()
        
        if not response.get("ok"):
            print(f"Could not list channels to check archive status: {response.get('error')}")
            return False
        
        channels = response.get("channels", [])
        for channel in channels:
            if channel.get("name") == channel_name:
                is_archived = channel.get("is_archived", False)
                print(f"Channel {channel_name} archived status: {is_archived}")
                return is_archived
        
        print(f"Channel {channel_name} not found in channel list")
        return False
        
    except Exception as e:
        print(f"Error checking channel archive status: {e}")
        return False

def cleanup_temp_lock_channel(channel_name):
    """Attempts to delete the temporary channel used for atomic lock."""
    try:
        print(f"Note: Cannot delete atomic lock channel {channel_name} due to permission restrictions")
        print(f"This is normal - the lock channel will remain but won't interfere with future operations")
    except Exception as e:
        print(f"Error in cleanup (expected): {e}")

def track_command_response(channel_id, user_id, command_text, response_ts):
    """Track a command response to prevent processing bot messages that are our responses"""
    if not DYNAMODB_AVAILABLE or not coordination_table:
        return
    
    try:
        # Calculate expiration time (1 hour from now)
        now = datetime.datetime.now()
        expiration_time = now + datetime.timedelta(hours=1)
        expiration_timestamp = int(expiration_time.timestamp())
        
        # Create a tracking key for this command response
        command_hash = hashlib.md5(command_text.encode()).hexdigest()[:8]
        tracking_key = f"cmd-response-{channel_id}-{user_id}-{command_hash}"
        
        coordination_table.put_item(
            Item={
                'incident_key': tracking_key,
                'command_text': command_text,
                'response_ts': response_ts,
                'tracked_at': now.isoformat(),
                'expiration_time': expiration_timestamp,
                'status': 'tracking'
            }
        )
        print(f"Tracked command response: {tracking_key}")
        
    except Exception as e:
        print(f"Error tracking command response: {e}")

def is_our_command_response(event_data):
    """Check if this bot message is a response to our command"""
    if not DYNAMODB_AVAILABLE or not coordination_table:
        return False
    
    try:
        channel_id = event_data.get("channel", "")
        user_id = event_data.get("user", "")
        text = event_data.get("text", "")
        event_ts = event_data.get("ts", "")
        
        # Check if this is a bot message
        if not (event_data.get("bot_id") or event_data.get("app_id")):
            return False
        
        # For now, use a simpler approach - check if this is our bot's response
        # We can enhance this later with more sophisticated tracking
        bot_user_ids = [os.environ.get("SLACK_BOT_USER_ID"), "U09584DT15X"]
        if user_id in [uid for uid in bot_user_ids if uid]:
            print(f"Detected our bot's response message: {text[:50]}...")
            return True
        
        return False
        
    except Exception as e:
        print(f"Error checking if our command response: {e}")
        return False

def post_incident_channel_greeting(channel_id, issue_key):
    """Post a greeting message to the incident channel with AI command information"""
    try:
        # Fetch latest ticket data
        jira_data = fetch_jira_data(issue_key)
        if jira_data.status_code != 200:
            print(f"Warning: Could not fetch latest ticket data for greeting: {jira_data.text}")
            ticket_info = None
        else:
            ticket_info = parse_jira_ticket(jira_data.json())
        
        # Build ticket details section
        ticket_details = f"🔗 **Jira Ticket:** <https://{JIRA_DOMAIN}/browse/{issue_key}|{issue_key}>"
        
        # Add affected hospitals/practices if available
        if ticket_info and ticket_info.get('hospitals'):
            hospitals_list = ", ".join(ticket_info['hospitals'])
            ticket_details += f"\n🏥 **Affected Practice{'s' if len(ticket_info['hospitals']) > 1 else ''}:** {hospitals_list}"
    
        greeting_text = f"""🚨 **Welcome to the incident channel for {issue_key}!** 🚨

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{ticket_details}

I'm FireBot 🤖, your AI-powered incident management assistant. Here's what I can help you with:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🎯 **AI Commands Available:**
• `firebot summary` 📋 - Generate a comprehensive AI summary of the incident
• `firebot time` ⏰ - Show how long the incident has been open
• `firebot timeline` 📊 - Generate a detailed timeline of events and response metrics
• `firebot resolve` ✅ - Mark incident as resolved and post summary to Jira ticket

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📚 **Helpful Resources:**
• 📖 <https://www.notion.so/instinctvet/Production-Support-Technical-How-Tos-d1c221f62ca64ce1ba76885fb8190aeb|Production Support Technical How-Tos> - Common troubleshooting steps
• 🔄 <https://instinctual.instinctvet.com|Instinctual> - Access customer instances for testing
• 📊 <https://app.datadoghq.com/logs|Datadog Logs> - View application and system logs
• 👥 Use `/jsmops all schedules` to see who's currently on-call

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

👨‍💻 An engineer will be joining shortly to help investigate and resolve this incident. Don't worry if you don't see them immediately - our escalation system ensures someone will respond.

Just type one of the commands above to get started! I'm here to help make incident management more efficient. 🐾"""

        # Post the greeting message
        response = post_message(channel_id, greeting_text)
        print(f"Posted greeting message to channel {channel_id}")
        return response
        
    except Exception as e:
        print(f"Error posting greeting message: {e}")
        return None

def update_jira_with_slack_link(issue_key, channel_name, channel_id):
    """Updates the Jira ticket with a link to the Slack incident channel"""
    try:
        url = f"https://{JIRA_DOMAIN}/rest/api/3/issue/{issue_key}/comment"
        
        # Create the comment in Atlassian Document Format (ADF)
        comment_body = {
            "body": {
                "version": 1,
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": "🔗 Slack incident channel created: "
                            },
                            {
                                "type": "text",
                                "text": f"#{channel_name}",
                                "marks": [
                                    {
                                        "type": "link",
                                        "attrs": {
                                            "href": f"slack://channel?team=T024F9QG2&id={channel_id}"
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        }
        
        response = requests.post(
            url,
            auth=(FIREBOT_JIRA_USERNAME, FIREBOT_JIRA_API_TOKEN),
            headers={"Content-Type": "application/json"},
            json=comment_body
        )
        
        if response.status_code == 201:
            print(f"Successfully added Slack channel link to Jira ticket {issue_key}")
        else:
            print(f"Failed to update Jira ticket with Slack link: {response.status_code} - {response.text}")
            
    except Exception as e:
        print(f"Error updating Jira ticket with Slack link: {e}")

def handle_firebot_timeline(channel_id, user_id):
    """Generate a detailed timeline of events in the incident channel"""
    try:
        print(f"Generating incident timeline for channel {channel_id}")
        
        # Get channel info for creation time
        channel_info = get_channel_info(channel_id)
        if not channel_info:
            response_ts = post_message(channel_id, "Could not retrieve channel information.")
            return response_ts
        
        created_timestamp = channel_info.get("created", 0)
        channel_name = channel_info.get("name", "")
        
        # Get complete channel history
        messages = get_channel_history(channel_id, limit=1000)  # Get more messages for timeline
        if not messages:
            response_ts = post_message(channel_id, "Could not retrieve channel history for timeline.")
            return response_ts
        
        # Sort messages by timestamp
        messages.sort(key=lambda x: float(x.get("ts", 0)))
        
        # Track key events and metrics
        timeline_data = analyze_channel_timeline(messages, created_timestamp, channel_id)
        
        # Generate timeline message
        timeline_message = format_timeline_message(timeline_data, channel_name)
        
        # Post the timeline
        response_ts = post_message(channel_id, timeline_message)
        return response_ts
        
    except Exception as e:
        print(f"Error generating incident timeline: {e}")
        response_ts = post_message(channel_id, "Sorry, I encountered an error while generating the timeline.")
        return response_ts

def analyze_channel_timeline(messages, created_timestamp, channel_id):
    """Analyze channel messages to create a timeline of events"""
    timeline_data = {
        "created_time": datetime.datetime.fromtimestamp(created_timestamp),
        "first_response_time": None,
        "resolution_time": None,
        "total_duration": None,
        "key_events": [],
        "participants": set(),
        "bot_user_ids": [os.environ.get("SLACK_BOT_USER_ID"), "U09584DT15X"],
        "first_engineer_response": None,
        "ticket_creator": None,
        "first_engineer": None
    }
    
    # Track when users join
    joined_users = set()
    
    # First pass: Find the ticket creator (first non-bot user to post)
    for msg in messages:
        user_id = msg.get("user", "")
        if (user_id and 
            user_id not in timeline_data["bot_user_ids"] and 
            not msg.get("bot_id") and 
            not msg.get("app_id")):
            timeline_data["ticket_creator"] = user_id
            break
    
    print(f"Identified ticket creator: {timeline_data['ticket_creator']}")
    
    # Second pass: Analyze timeline
    for msg in messages:
        timestamp = float(msg.get("ts", 0))
        msg_time = datetime.datetime.fromtimestamp(timestamp)
        user_id = msg.get("user", "")
        text = msg.get("text", "").lower()  # Convert to lowercase for easier matching
        original_text = msg.get("text", "")  # Keep original text for summaries
        subtype = msg.get("subtype", "")
        
        # Skip bot messages for participant tracking
        if user_id in timeline_data["bot_user_ids"] or msg.get("bot_id") or msg.get("app_id"):
            # But still analyze bot messages for key events
            if "incident channel created" in text:
                timeline_data["key_events"].append({
                    "time": msg_time,
                    "event": "Incident Channel Created",
                    "details": "Bot created incident channel"
                })
            elif "uploaded" in text and "media file" in text:
                timeline_data["key_events"].append({
                    "time": msg_time,
                    "event": "Media Uploaded",
                    "details": "Screenshots/media files uploaded from Jira"
                })
            # Add detection for resolution message from firebot resolve command
            elif "✅ this incident has been marked as resolved" in text:
                timeline_data["resolution_time"] = msg_time
                timeline_data["key_events"].append({
                    "time": msg_time,
                    "event": "Resolution",
                    "details": "Incident marked as resolved via firebot resolve command"
                })
            continue
        
        # Track channel joins
        if subtype == "channel_join" and user_id not in joined_users:
            joined_users.add(user_id)
            # Get user info for better display
            user_info = get_user_info(user_id)
            display_name = user_info.get("real_name", user_id) if user_info else user_id
            
            # Distinguish between creator and engineer joins
            if user_id == timeline_data["ticket_creator"]:
                timeline_data["key_events"].append({
                    "time": msg_time,
                    "event": "Creator Joined",
                    "details": f"Ticket creator {display_name} joined the channel"
                })
            else:
                timeline_data["key_events"].append({
                    "time": msg_time,
                    "event": "Engineer Joined",
                    "details": f"Engineer {display_name} joined the channel"
                })
                if not timeline_data["first_engineer"]:
                    timeline_data["first_engineer"] = user_id
        
        # Track first engineer response (first message from an engineer after they join)
        if (not timeline_data["first_engineer_response"] and 
            user_id not in timeline_data["bot_user_ids"] and
            user_id != timeline_data["ticket_creator"]):
            
            # Only count as engineer response if they've joined the channel
            if user_id in joined_users:
                timeline_data["first_engineer_response"] = msg_time
                user_info = get_user_info(user_id)
                display_name = user_info.get("real_name", user_id) if user_info else user_id
                # Include the actual response content
                timeline_data["key_events"].append({
                    "time": msg_time,
                    "event": "First Engineer Response",
                    "details": f"{display_name}: {original_text}"
                })
        
        # Track resolution indicators
        resolution_keywords = ["resolved", "fixed", "solution", "closing", "completed", "firebot resolve"]
        if any(keyword in text for keyword in resolution_keywords):
            timeline_data["resolution_time"] = msg_time
            user_info = get_user_info(user_id)
            display_name = user_info.get("real_name", user_id) if user_info else user_id
            timeline_data["key_events"].append({
                "time": msg_time,
                "event": "Resolution Update",
                "details": f"{display_name}: {original_text}"
            })
        
        # Track investigation activities with content summary
        investigation_keywords = ["investigating", "checked", "found", "tested", "reproduced", "identified", "confirmed", "verified", "discovered"]
        if any(keyword in text for keyword in investigation_keywords):
            user_info = get_user_info(user_id)
            display_name = user_info.get("real_name", user_id) if user_info else user_id
            
            # Clean up the message for better readability
            update_text = original_text
            # Remove common Slack formatting
            update_text = re.sub(r'<@[A-Z0-9]+>', '', update_text)  # Remove user mentions
            update_text = re.sub(r'<#[A-Z0-9]+\|[^>]+>', '', update_text)  # Remove channel mentions
            update_text = re.sub(r'<https?://[^>]+>', '', update_text)  # Remove links
            update_text = update_text.strip()
            
            timeline_data["key_events"].append({
                "time": msg_time,
                "event": "Investigation Update",
                "details": f"{display_name}: {update_text}"
            })
        
        # Add to participants set
        if user_id and user_id not in timeline_data["bot_user_ids"]:
            timeline_data["participants"].add(user_id)
    
    # Calculate response metrics
    if timeline_data["first_engineer_response"]:
        response_time = timeline_data["first_engineer_response"] - timeline_data["created_time"]
        timeline_data["first_response_time"] = response_time
    
    if timeline_data["resolution_time"]:
        total_time = timeline_data["resolution_time"] - timeline_data["created_time"]
        timeline_data["total_duration"] = total_time
    
    return timeline_data

def format_timeline_message(timeline_data, channel_name):
    """Format the timeline data into a readable message"""
    created_time = timeline_data["created_time"]
    
    # Format header
    header = f"📊 **Incident Timeline for #{channel_name}** 📊\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    
    # Format response metrics
    metrics = ["\n⏱️ **Response Metrics:**"]
    if timeline_data["first_response_time"]:
        metrics.append(f"• 🔄 Time to First Engineer Response: {format_duration(timeline_data['first_response_time'])}")
    if timeline_data["total_duration"]:
        metrics.append(f"• ⌛ Total Resolution Time: {format_duration(timeline_data['total_duration'])}")
    metrics.append(f"• 📅 Incident Start: {created_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    
    # Add note if no engineer response yet
    if not timeline_data["first_response_time"]:
        metrics.append("• ⚠️ No engineer response detected yet")
    
    metrics.append("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    
    # Format participants
    participants = []
    for user_id in timeline_data["participants"]:
        user_info = get_user_info(user_id)
        if user_info:
            participants.append(user_info.get("real_name", user_id))
        else:
            participants.append(user_id)
    
    participant_section = "\n\n👥 **Participants:**\n• 👤 " + "\n• 👤 ".join(participants)
    participant_section += "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    
    # Format timeline events
    timeline_events = ["\n\n⏰ **Event Timeline:**"]
    sorted_events = sorted(timeline_data["key_events"], key=lambda x: x["time"])
    
    for event in sorted_events:
        time_str = event["time"].strftime("%H:%M:%S UTC")
        # Add emoji based on event type
        emoji = "🔵"  # Default
        if "joined" in event["event"].lower():
            emoji = "➡️"
        elif "created" in event["event"].lower():
            emoji = "🆕"
        elif "uploaded" in event["event"].lower():
            emoji = "📎"
        elif "response" in event["event"].lower():
            emoji = "💬"
        elif "resolution" in event["event"].lower():
            emoji = "✅"
        elif "investigation" in event["event"].lower():
            emoji = "🔍"
        timeline_events.append(f"• {emoji} {time_str} - {event['event']}: {event['details']}")
    
    # Combine all sections
    message = "\n".join([
        header,
        "\n".join(metrics),
        participant_section,
        "\n".join(timeline_events)
    ])
    
    return message

def get_user_info(user_id):
    """Get user information from Slack"""
    try:
        response = requests.get(
            "https://slack.com/api/users.info",
            headers=SLACK_HEADERS,
            params={"user": user_id}
        ).json()
        
        if response.get("ok"):
            return response.get("user", {})
        else:
            print(f"Could not get user info: {response.get('error')}")
            return None
            
    except Exception as e:
        print(f"Error getting user info: {e}")
        return None

def handle_firebot_resolve(channel_id, user_id):
    """Handle the firebot resolve command"""
    try:
        print(f"Processing resolve command for channel {channel_id}")
        
        # Get channel info to extract issue key
        channel_info = get_channel_info(channel_id)
        if not channel_info:
            response_ts = post_message(channel_id, "Could not retrieve channel information.")
            return response_ts
        
        channel_name = channel_info.get("name", "")
        
        # Extract issue key from channel name
        issue_match = re.search(r"incident-(isd-\d{5})", channel_name.lower())
        if not issue_match:
            response_ts = post_message(channel_id, "Could not determine the Jira issue key from channel name.")
            return response_ts
        
        issue_key = issue_match.group(1).upper()
        print(f"Found issue key: {issue_key}")
        
        # Generate comprehensive summary including timeline
        summary = generate_resolution_summary(channel_id, issue_key)
        if not summary:
            response_ts = post_message(channel_id, "Could not generate resolution summary.")
            return response_ts
        
        # Post summary to Jira
        jira_comment = post_resolution_to_jira(issue_key, summary, channel_id)
        if not jira_comment:
            response_ts = post_message(channel_id, "Could not post resolution summary to Jira ticket.")
            return response_ts
        
        # Generate resolution message
        resolution_message = generate_resolution_message(issue_key, channel_id)
        
        # Post resolution message
        response_ts = post_message(channel_id, resolution_message)
        return response_ts
        
    except Exception as e:
        print(f"Error handling resolve command: {e}")
        response_ts = post_message(channel_id, "Sorry, I encountered an error while resolving the incident.")
        return response_ts

def generate_resolution_summary(channel_id, issue_key):
    """Generate a comprehensive resolution summary"""
    try:
        # Get channel info and history
        channel_info = get_channel_info(channel_id)
        if not channel_info:
            return None
        
        created_timestamp = channel_info.get("created", 0)
        messages = get_channel_history(channel_id, limit=1000)
        if not messages:
            return None
        
        # Generate timeline data
        timeline_data = analyze_channel_timeline(messages, created_timestamp, channel_id)
        
        # Generate summary using AI
        summary = generate_incident_resolution_summary(messages, timeline_data, issue_key)
        
        return summary
        
    except Exception as e:
        print(f"Error generating resolution summary: {e}")
        return None

def generate_incident_resolution_summary(messages, timeline_data, issue_key):
    """Generate an AI-powered resolution summary"""
    try:
        # Format messages for AI analysis
        formatted_messages = []
        for msg in messages:
            user_id = msg.get("user", "Unknown")
            text = msg.get("text", "")
            timestamp = msg.get("ts", "")
            
            # Skip bot messages
            if (user_id in timeline_data["bot_user_ids"] or 
                msg.get("bot_id") or 
                msg.get("app_id")):
                continue
            
            # Look up user info for proper display name
            user_info = get_user_info(user_id)
            display_name = user_info.get("real_name", user_id) if user_info else user_id
            
            if timestamp:
                time_str = datetime.datetime.fromtimestamp(float(timestamp)).strftime('%H:%M:%S')
            else:
                time_str = "Unknown"
            
            formatted_messages.append(f"[{time_str}] {display_name}: {text}")
        
        # Limit to last 50 messages to avoid token limits
        recent_messages = formatted_messages[-50:]
        messages_text = "\n".join(recent_messages)
        
        # Format timeline metrics
        metrics = []
        if timeline_data["first_response_time"]:
            metrics.append(f"Time to First Response: {format_duration(timeline_data['first_response_time'])}")
        if timeline_data["total_duration"]:
            metrics.append(f"Total Resolution Time: {format_duration(timeline_data['total_duration'])}")
        
        metrics_text = "\n".join(metrics)
        
        # Filter out bot users from participants
        human_participants = [
            user_id for user_id in timeline_data["participants"]
            if user_id not in timeline_data["bot_user_ids"]
        ]
        
        # Get display names for human participants
        participant_names = []
        for user_id in human_participants:
            user_info = get_user_info(user_id)
            if user_info:
                participant_names.append(user_info.get("real_name", user_id))
            else:
                participant_names.append(user_id)
        
        participants_text = ", ".join(participant_names) if participant_names else "No participants recorded"
        
        prompt = f"""You are an incident management assistant. Generate a comprehensive resolution summary for this incident.

Issue: {issue_key}

Timeline Metrics:
{metrics_text}

Participants: {participants_text}

Recent Channel Activity:
{messages_text}

Please provide a structured summary that includes:
1. Root Cause: What was the underlying issue?
2. Impact: Who was affected and how severely?
3. Resolution: How was the issue resolved?
4. Key Actions: What were the main steps taken?
5. Timeline: Brief timeline of key events
6. Participants: Who was involved in resolution (excluding bots)
7. Recommendations: Any follow-up actions needed

Keep it professional and factual. Focus on the most important information for documentation."""

        fallback_models = ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-pro"]
        models_to_try = [GEMINI_MODEL] + [m for m in fallback_models if m != GEMINI_MODEL]
        
        for model_name in models_to_try:
            try:
                print(f"Generating resolution summary with model: {model_name}")
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(prompt)
                
                if hasattr(response, 'text') and response.text:
                    print(f"Successfully generated resolution summary with model: {model_name}")
                    return response.text.strip()
                elif response.parts:
                    summary_text = ''.join(part.text for part in response.parts if hasattr(part, 'text'))
                    if summary_text:
                        print(f"Successfully generated resolution summary with model: {model_name}")
                        return summary_text.strip()
                
            except Exception as e:
                print(f"Error with model {model_name}: {e}")
                continue
        
        return "Could not generate resolution summary."
        
    except Exception as e:
        print(f"Error generating resolution summary: {e}")
        return None

def post_resolution_to_jira(issue_key, summary, channel_id):
    """Post resolution summary to Jira ticket"""
    try:
        url = f"https://{JIRA_DOMAIN}/rest/api/3/issue/{issue_key}/comment"
        
        # Get channel info to calculate total duration
        channel_info = get_channel_info(channel_id)
        duration_text = ""
        if channel_info:
            created_ts = float(channel_info.get("created", 0))
            if created_ts > 0:
                created_time = datetime.datetime.fromtimestamp(created_ts)
                resolution_time = datetime.datetime.now()
                total_duration = resolution_time - created_time
                duration_text = f"\n\n⏱️ Total Resolution Time: {format_duration(total_duration)}"
        
        # Create the comment in Atlassian Document Format (ADF)
        comment_body = {
            "body": {
                "version": 1,
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": "🏁 Incident Resolution Summary\n\n"
                            }
                        ]
                    },
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": summary + duration_text
                            }
                        ]
                    }
                ]
            }
        }
        
        response = requests.post(
            url,
            auth=(FIREBOT_JIRA_USERNAME, FIREBOT_JIRA_API_TOKEN),
            headers={"Content-Type": "application/json"},
            json=comment_body
        )
        
        if response.status_code == 201:
            print(f"Successfully posted resolution summary to Jira ticket {issue_key}")
            return response.json()
        else:
            print(f"Failed to post resolution summary: {response.status_code} - {response.text}")
            return None
            
    except Exception as e:
        print(f"Error posting resolution to Jira: {e}")
        return None

def check_if_postmortem_needed(channel_id):
    """Check if a post-mortem is needed based on incident duration and severity"""
    try:
        # Get channel info and history
        channel_info = get_channel_info(channel_id)
        if not channel_info:
            return False
        
        created_timestamp = channel_info.get("created", 0)
        messages = get_channel_history(channel_id)
        if not messages:
            return False
        
        # Generate timeline data
        timeline_data = analyze_channel_timeline(messages, created_timestamp, channel_id)
        
        # Check duration - if over 2 hours, suggest post-mortem
        if timeline_data["total_duration"]:
            duration_hours = timeline_data["total_duration"].total_seconds() / 3600
            if duration_hours >= 2:
                return True
        
        # Check message volume - if high discussion volume, suggest post-mortem
        if len(messages) > 100:  # Arbitrary threshold
            return True
        
        # Check participant count - if many people involved, suggest post-mortem
        if len(timeline_data["participants"]) > 5:  # Arbitrary threshold
            return True
        
        return False
        
    except Exception as e:
        print(f"Error checking if post-mortem needed: {e}")
        return False

def generate_resolution_message(issue_key, channel_id):
    """Generate the resolution message for Slack"""
    try:
        # Get channel info to extract creation time
        channel_info = get_channel_info(channel_id)
        if not channel_info:
            return "\n".join([
                f"✅ This incident has been marked as resolved.",
                f"A comprehensive summary and timeline have been posted to the Jira ticket: <https://{JIRA_DOMAIN}/browse/{issue_key}|{issue_key}>",
                "",
                "🔍 **Post-Mortem Reminder**",
                "If this incident requires a post-mortem, please remember to:",
                "• Schedule a meeting with the relevant team members",
                "• Document key findings and action items",
                "• Update the ticket with post-mortem notes",
                "",
                "Thank you to everyone who helped resolve this incident! 🙌"
            ])

        # Calculate total duration
        created_ts = float(channel_info.get("created", 0))
        if created_ts > 0:
            created_time = datetime.datetime.fromtimestamp(created_ts)
            resolution_time = datetime.datetime.now()
            total_duration = resolution_time - created_time
            duration_str = format_duration(total_duration)
            
            message = [
                f"✅ This incident has been marked as resolved.",
                f"⏱️ Total resolution time: {duration_str}",
                f"A comprehensive summary and timeline have been posted to the Jira ticket: <https://{JIRA_DOMAIN}/browse/{issue_key}|{issue_key}>",
                "",
                "🔍 **Post-Mortem Reminder**",
                "If this incident requires a post-mortem, please remember to:",
                "• Schedule a meeting with the relevant team members",
                "• Document key findings and action items",
                "• Update the ticket with post-mortem notes",
                "",
                "Thank you to everyone who helped resolve this incident! 🙌"
            ]
        else:
            message = [
                f"✅ This incident has been marked as resolved.",
                f"A comprehensive summary and timeline have been posted to the Jira ticket: <https://{JIRA_DOMAIN}/browse/{issue_key}|{issue_key}>",
                "",
                "🔍 **Post-Mortem Reminder**",
                "If this incident requires a post-mortem, please remember to:",
                "• Schedule a meeting with the relevant team members",
                "• Document key findings and action items",
                "• Update the ticket with post-mortem notes",
                "",
                "Thank you to everyone who helped resolve this incident! 🙌"
            ]
        
        return "\n".join(message)
    except Exception as e:
        print(f"Error generating resolution message: {e}")
        # Fallback to basic message
        return "\n".join([
            f"✅ This incident has been marked as resolved.",
            f"A comprehensive summary and timeline have been posted to the Jira ticket: <https://{JIRA_DOMAIN}/browse/{issue_key}|{issue_key}>",
            "",
            "Thank you to everyone who helped resolve this incident! 🙌"
        ])
