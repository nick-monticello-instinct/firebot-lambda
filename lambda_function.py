import json
import os
import re
import datetime
import requests
import openai

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
JIRA_USERNAME = os.environ["JIRA_USERNAME"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
JIRA_DOMAIN = os.environ["JIRA_DOMAIN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

openai.api_key = OPENAI_API_KEY

SLACK_HEADERS = {
    "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
    "Content-Type": "application/json"
}

def lambda_handler(event, context=None):
    try:
        print("Incoming event:", json.dumps(event))
        if event.get("body"):
            body = json.loads(event["body"])

            # Slack URL verification
            if body.get("type") == "url_verification":
                return {
                    "statusCode": 200,
                    "body": body.get("challenge")
                }

            if body.get("type") == "event_callback":
                user_id = body["event"]["user"]
                text = body["event"]["text"]
                print("Processing Slack message text:", text)
                process_fire_ticket(body, user_id)
                return {"statusCode": 200, "body": "OK"}

        return {"statusCode": 400, "body": "Bad request"}

    except Exception as e:
        print("Unhandled exception in lambda_handler", e)
        return {"statusCode": 500, "body": str(e)}

def process_fire_ticket(event_data, user_id):
    text = event_data["event"]["text"]
    issue_match = re.search(r"(ISD-\d+)", text)
    if not issue_match:
        print("No Jira issue key found.")
        return

    issue_key = issue_match.group(1)
    jira_data = fetch_jira_data(issue_key)
    print("Jira API response status:", jira_data.status_code)
    if jira_data.status_code != 200:
        raise Exception("Failed to fetch Jira ticket data")

    ticket = jira_data.json()
    parsed = parse_jira_ticket(ticket)
    summary = generate_gpt_summary(parsed)

    date_str = datetime.datetime.now().strftime("%Y%m%d")
    channel_slug = re.sub(r"[^a-z0-9\-]", "", parsed["hospital"].lower())
    channel_name = f"incident-{date_str}-{channel_slug}"

    channel_id = get_or_create_channel(channel_name)
    invite_user_to_channel(user_id, channel_id)
    post_welcome_message(event_data["event"]["channel"], channel_name)

def fetch_jira_data(issue_key):
    return requests.get(
        f"https://{JIRA_DOMAIN}/rest/api/3/issue/{issue_key}",
        auth=(JIRA_USERNAME, JIRA_API_TOKEN),
        headers={"Accept": "application/json"}
    )

def parse_jira_ticket(ticket):
    fields = ticket.get("fields", {})
    hospital = fields.get("customfield_12345", "unknown-hospital")  # Replace with real field
    summary = fields.get("summary", "")
    description = fields.get("description", "")
    return {"hospital": hospital, "summary": summary, "description": description}

def generate_gpt_summary(data):
    try:
        prompt = f"Summarize this incident:\n\nSummary: {data['summary']}\n\nDescription: {data['description']}"
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message["content"]
    except Exception as e:
        print("Error generating GPT summary:", e)
        return ""

def get_or_create_channel(name):
    # Check if channel already exists
    result = requests.get("https://slack.com/api/conversations.list", headers=SLACK_HEADERS).json()
    if result.get("ok"):
        for ch in result["channels"]:
            if ch["name"] == name:
                print(f"Channel '{name}' already exists.")
                return ch["id"]

    # Create the public channel
    response = requests.post(
        "https://slack.com/api/conversations.create",
        headers=SLACK_HEADERS,
        json={"name": name, "is_private": False}
    ).json()

    if response.get("ok"):
        print(f"Channel '{name}' created.")
        return response["channel"]["id"]
    else:
        raise Exception(f"Failed to create or retrieve channel: {response}")

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
        "text": f":rotating_light: I've created <#{new_channel_name}> for this incident. Please move all comms there. :rotating_light:"
    })
    if not response.ok:
        print("Error posting welcome message:", response.text)
