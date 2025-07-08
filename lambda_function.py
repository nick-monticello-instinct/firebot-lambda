import json
import os
import re
import datetime
import requests
from openai import OpenAI  # openai>=1.0.0

# ENVIRONMENT VARIABLES
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
JIRA_USERNAME = os.environ["JIRA_USERNAME"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
JIRA_DOMAIN = os.environ["JIRA_DOMAIN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

client = OpenAI()  # Do NOT pass api_key here, pass it in the request instead

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
    base_channel_name = f"incident-{date_str}-{channel_slug}"
    channel_id, channel_name = create_incident_channel(base_channel_name)

    invite_user_to_channel(user_id, channel_id)
    post_welcome_message(event_data["event"]["channel"], channel_name)
    post_summary_message(channel_id, summary)


def fetch_jira_data(issue_key):
    return requests.get(
        f"https://{JIRA_DOMAIN}/rest/api/3/issue/{issue_key}",
        auth=(JIRA_USERNAME, JIRA_API_TOKEN),
        headers={"Accept": "application/json"}
    )


def parse_jira_ticket(ticket):
    fields = ticket.get("fields", {})
    hospital = fields.get("customfield_12345", "unknown-hospital")  # Replace with real field ID
    summary = fields.get("summary", "")
    description = fields.get("description", "")
    return {"hospital": hospital, "summary": summary, "description": description}


def generate_gpt_summary(data):
    try:
        prompt = f"""You are a helpful assistant summarizing incident tickets.

Summary:
{data['summary']}

Description:
{data['description']}

Please provide a concise summary in plain English suitable for a Slack incident channel."""

        response = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "You summarize Jira incidents for engineers."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.5,
            api_key=OPENAI_API_KEY  # ✅ pass key here instead of client init
        )

        return response.choices[0].message.content.strip()

    except Exception as e:
        print("Error generating GPT summary:", e)
        return "GPT summary could not be generated."


def create_incident_channel(base_name, attempt=0):
    name = base_name if attempt == 0 else f"{base_name}-{attempt}"
    response = requests.post(
        "https://slack.com/api/conversations.create",
        headers=SLACK_HEADERS,
        json={"name": name, "is_private": False}  # ✅ Public channel
    ).json()

    if response.get("ok"):
        return response["channel"]["id"], response["channel"]["name"]
    elif response.get("error") == "name_taken" and attempt < 10:
        return create_incident_channel(base_name, attempt + 1)
    else:
        raise Exception(f"Failed to create channel: {response}")


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


def post_summary_message(channel_id, summary):
    response = requests.post("https://slack.com/api/chat.postMessage", headers=SLACK_HEADERS, json={
        "channel": channel_id,
        "text": f"*Incident Summary:*\n{summary}"
    })
    if not response.ok:
        print("Error posting GPT summary message:", response.text)
