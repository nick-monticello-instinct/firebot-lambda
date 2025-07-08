import json
import os
import re
import datetime
import requests
import google.generativeai as genai  # Gemini SDK
from threading import Thread

# ENVIRONMENT VARIABLES
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
JIRA_USERNAME = os.environ["JIRA_USERNAME"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
JIRA_DOMAIN = os.environ["JIRA_DOMAIN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-pro")
JIRA_HOSPITAL_FIELD = os.environ.get("JIRA_HOSPITAL_FIELD", "customfield_12345")
JIRA_SUMMARY_FIELD = "customfield_10250"  # Custom summary form

# Configure Gemini client
genai.configure(api_key=GEMINI_API_KEY)

SLACK_HEADERS = {
    "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
    "Content-Type": "application/json"
}

PROCESSED_EVENT_IDS = set()

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
                user_id = body["event"].get("user")
                text = body["event"].get("text", "")
                print("Slack message text:", text)
                try:
                    process_fire_ticket(body, user_id)
                except Exception as err:
                    print("Error during fire ticket processing:", err)
                    return {"statusCode": 500, "body": str(err)}
                return {"statusCode": 200, "body": "OK"}

        return {"statusCode": 400, "body": "Bad request"}

    except Exception as e:
        print("Unhandled exception in lambda_handler:", e)
        return {"statusCode": 500, "body": str(e)}

def process_fire_ticket(event_data, user_id):
    text = event_data["event"].get("text", "")
    issue_match = re.search(r"(ISD-\d{5})", text)
    if not issue_match:
        print("No Jira issue key found in text:", text)
        return

    issue_key = issue_match.group(1)
    jira_data = fetch_jira_data(issue_key)
    print("Jira API response status:", jira_data.status_code)
    if jira_data.status_code != 200:
        raise Exception("Failed to fetch Jira ticket data")

    ticket = jira_data.json()
    parsed = parse_jira_ticket(ticket)
    summary = generate_gemini_summary(parsed)

    date_str = datetime.datetime.now().strftime("%Y%m%d")
    channel_slug = issue_key.lower()
    base_channel_name = f"incident-{channel_slug}-{date_str}"
    channel_id, channel_name = create_incident_channel(base_channel_name)

    invite_user_to_channel(user_id, channel_id)
    post_welcome_message(event_data["event"]["channel"], channel_name)
    post_summary_message(channel_id, summary)

def fetch_jira_data(issue_key):
    url = f"https://{JIRA_DOMAIN}/rest/api/3/issue/{issue_key}"
    print(f"Fetching Jira ticket from URL: {url}")
    response = requests.get(
        url,
        auth=(JIRA_USERNAME, JIRA_API_TOKEN),
        headers={"Accept": "application/json"}
    )
    print("Jira response status:", response.status_code)
    print("Jira response body:", response.text)
    return response

def parse_jira_ticket(ticket):
    fields = ticket.get("fields", {})
    hospital = fields.get(JIRA_HOSPITAL_FIELD, "unknown-hospital")
    summary = fields.get(JIRA_SUMMARY_FIELD, "")
    description = fields.get("description", "")
    return {"hospital": hospital, "summary": summary, "description": description}

from google.generativeai import GenerativeModel, configure

def generate_gemini_summary(data):
    """
    Generates a summary of a Jira ticket using the Gemini API.
    """
    try:
        # The genai client is already configured globally, no need to re-configure here.
        model = genai.GenerativeModel(GEMINI_MODEL)

        prompt = f"""You are a helpful assistant summarizing incident tickets.

Summary:
{data['summary']}

Description:
{data['description']}

Please provide a concise summary in plain English suitable for a Slack incident channel."""

        response = model.generate_content(prompt)

        # Simplified and safer way to get the text
        if response.parts:
            summary_text = ''.join(part.text for part in response.parts)
        else:
            # Fallback for cases where response.text might be directly available
            summary_text = response.text if hasattr(response, 'text') and response.text else ""

        if not summary_text:
            print("Empty Gemini response")
            return "Gemini summary could not be generated."

        return summary_text.strip()

    except Exception as e:
        print(f"Error generating Gemini summary: {e}")
        return "Gemini summary could not be generated due to an error."
        
def create_incident_channel(base_name):
    original_name = base_name.lower()

    # Fetch all channels (including archived)
    response = requests.get(
        "https://slack.com/api/conversations.list",
        headers=SLACK_HEADERS,
        params={"exclude_archived": "false", "limit": 1000}
    ).json()

    if not response.get("ok"):
        raise Exception(f"Failed to list Slack channels: {response}")

    existing_channels = {
        channel["name"]: channel for channel in response.get("channels", [])
    }

    # If exact name exists
    if original_name in existing_channels:
        channel = existing_channels[original_name]
        if not channel.get("is_archived", False):
            print(f"Reusing active channel: {original_name}")
            return channel["id"], original_name
        else:
            print(f"Found archived channel: {original_name}, falling back to new name")
            fallback_name = f"{original_name}-new"
            if fallback_name in existing_channels:
                fallback_channel = existing_channels[fallback_name]
                if not fallback_channel.get("is_archived", False):
                    print(f"Reusing fallback channel: {fallback_name}")
                    return fallback_channel["id"], fallback_name
                else:
                    raise Exception(f"Both original and fallback channel are archived. Cannot proceed.")
            else:
                print(f"Creating fallback channel: {fallback_name}")
                create = requests.post(
                    "https://slack.com/api/conversations.create",
                    headers=SLACK_HEADERS,
                    json={"name": fallback_name, "is_private": False}
                ).json()
                if create.get("ok"):
                    return create["channel"]["id"], fallback_name
                else:
                    raise Exception(f"Failed to create fallback channel: {create}")

    # If not found at all, create original
    print(f"No existing channel found. Creating: {original_name}")
    create = requests.post(
        "https://slack.com/api/conversations.create",
        headers=SLACK_HEADERS,
        json={"name": original_name, "is_private": False}
    ).json()

    if create.get("ok"):
        return create["channel"]["id"], original_name
    else:
        if create.get("error") == "name_taken":
            raise Exception(f"Channel name '{original_name}' taken but not listed. Manual cleanup might be needed.")
        raise Exception(f"Failed to create channel: {create}")
        
def invite_user_to_channel(user_id, channel_id):
    response = requests.post("https://slack.com/api/conversations.invite", headers=SLACK_HEADERS, json={
        "channel": channel_id,
        "users": user_id
    }).json()
    if not response.get("ok"):
        print(f"Warning: Could not invite user {user_id} to channel {channel_id}: {response}")

def post_welcome_message(source_channel, new_channel_name):
    response = requests.post("https://slack.com/api/chat.postMessage", headers=SLACK_HEADERS, json={
        "channel": source_channel,
        "text": f":rotating_light: I've created #{new_channel_name} for this incident. Please move all comms there. :rotating_light:"
    })
    response_data = response.json()
    if not response_data.get("ok"):
        print(f"Error posting welcome message: {response_data.get('error')}")

def post_summary_message(channel_id, summary):
    response = requests.post("https://slack.com/api/chat.postMessage", headers=SLACK_HEADERS, json={
        "channel": channel_id,
        "text": f"*Incident Summary:*\n{summary}"
    })
    response_data = response.json()
    if not response_data.get("ok"):
        print(f"Error posting summary message: {response_data.get('error')}")
