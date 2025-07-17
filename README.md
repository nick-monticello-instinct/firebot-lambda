# 🔥 FireBot - Intelligent Incident Management Bot

FireBot is an AWS Lambda-powered Slack bot designed to streamline incident management for veterinary software teams. When Jira incident tickets are mentioned in Slack, FireBot automatically creates dedicated incident channels, provides AI-powered analysis, uploads relevant media, and guides reporters through providing complete investigation details.

## ✨ Features

### 🤖 **Automated Incident Channel Management**
- **Smart Channel Creation**: Automatically creates incident channels when Jira tickets (ISD-XXXXX) are mentioned
- **Intelligent Naming**: Channels follow the pattern `incident-{ticket}-{date}-{hospital}`
- **User Invitation**: Automatically invites ticket reporters and mentioning users to incident channels
- **Deduplication**: Prevents duplicate processing using both in-memory cache and DynamoDB
- **Distributed Coordination**: Uses DynamoDB for reliable incident processing across multiple instances

### 🧠 **AI-Powered Ticket Analysis**
- **Gemini Integration**: Uses Google's Gemini AI to generate concise, actionable incident summaries
- **Intelligent Parsing**: Handles both plain text and Atlassian Document Format (ADF) descriptions
- **Fallback Models**: Automatic fallback between Gemini models (1.5-flash, 1.5-pro, pro) for reliability
- **Model Mapping**: Smart mapping of legacy model names to new versions

### ✅ **Investigation Checklist Analysis**
FireBot analyzes each ticket against 7 critical investigation items:
1. **Issue replication in customer's application**
2. **Issue replication on Demo instance**
3. **Steps to reproduce**
4. **Screenshots provided**
5. **Problem start time**
6. **Practice-wide impact**
7. **Multi-practice impact**

### 📎 **Media Attachment Processing**
- **Automatic Detection**: Finds and downloads images/videos from Jira tickets
- **Smart Upload**: Uses modern Slack file upload API (files.getUploadURLExternal)
- **Size Validation**: Enforces file size limits (100MB) and validates image integrity
- **Rich Context**: Uploads include author information and source ticket references

### 🎯 **Intelligent Creator Outreach**
- **User Lookup**: Finds ticket creators in Slack by email address
- **Structured Requests**: Provides specific, actionable requests for missing information
- **Encouraging Tone**: Uses supportive, collaborative messaging to reduce stress
- **Contextual Analysis**: Adapts requests based on available information

### 🛡️ **Robust Error Handling**
- **Graceful Degradation**: Individual feature failures don't break core functionality
- **Comprehensive Logging**: Detailed logging for troubleshooting and monitoring
- **Duplicate Prevention**: Smart caching with size management (1000 events max)
- **Bot Message Filtering**: Avoids processing its own messages and other bot responses

### 🤖 **Interactive Commands**
- **Channel Summary**: `firebot summary` - Generate comprehensive incident summaries using AI
- **Duration Tracking**: `firebot time` - Show how long the incident has been open
- **Timeline Analysis**: `firebot timeline` - Generate detailed timeline with response metrics
- **Resolution Handling**: `firebot resolve` - Mark incidents as resolved with automated Jira updates
- **Smart Detection**: Only responds to commands in incident channels
- **AI-Powered Analysis**: Uses Gemini to analyze channel history and provide insights

## 🚀 Getting Started

### Prerequisites
- AWS Lambda environment
- Slack workspace with bot permissions
- Jira Cloud instance with API access
- Google Cloud project with Gemini API access
- DynamoDB table for coordination (optional but recommended)

### Required Environment Variables

```bash
# Slack Configuration
SLACK_BOT_TOKEN=xoxb-your-slack-bot-token
SLACK_BOT_USER_ID=U1234567890  # Optional: helps prevent duplicate processing

# Jira Configuration  
JIRA_USERNAME=your-jira-email@company.com  # Legacy support
JIRA_API_TOKEN=your-jira-api-token        # Legacy support
FIREBOT_JIRA_USERNAME=your-jira-email@company.com  # Preferred
FIREBOT_JIRA_API_TOKEN=your-jira-api-token        # Preferred
JIRA_DOMAIN=yourcompany.atlassian.net
JIRA_HOSPITAL_FIELD=customfield_10297  # Hospital/practice field ID
JIRA_SUMMARY_FIELD=customfield_10250   # Summary field ID

# AI Configuration
GEMINI_API_KEY=your-gemini-api-key
GEMINI_MODEL=gemini-1.5-flash  # Optional: defaults to gemini-1.5-flash

# DynamoDB Configuration
DYNAMODB_TABLE_NAME=firebot-coordination  # Optional: defaults to firebot-coordination
DYNAMODB_REGION=us-east-2                # Optional: defaults to us-east-2
```

### Required Slack Permissions

Your Slack app needs these OAuth scopes:

```
channels:read          # List channels
channels:write         # Create channels  
channels:manage        # Invite users to channels
chat:write            # Post messages
users:read.email      # Lookup users by email
users:read            # Get user information
files:write           # Upload media files
files:read            # Read file information
groups:history        # Read channel history for firebot commands
channels:history      # Read channel history for firebot commands
```

### Required AWS Resources

#### DynamoDB Table

Create a DynamoDB table for distributed coordination:

```bash
# Deploy the CloudFormation template
aws cloudformation deploy \
  --template-file dynamodb-table.yaml \
  --stack-name firebot-coordination \
  --capabilities CAPABILITY_IAM
```

#### Lambda IAM Permissions

Add these permissions to your Lambda execution role:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "dynamodb:DescribeTable",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem"
      ],
      "Resource": "arn:aws:dynamodb:*:*:table/firebot-coordination"
    }
  ]
}
```

### Dependencies

Install the required Python packages:

```bash
pip install -r requirements.txt
```

## 🏗️ Architecture

FireBot uses a distributed, event-driven architecture to process incidents reliably:

```mermaid
graph TD
    A[Slack Event] -->|Message with ISD-XXXXX| B[AWS Lambda FireBot]
    B -->|Check Cache| C[In-Memory Cache]
    B -->|Check/Update Lock| D[DynamoDB]
    B -->|Fetch Ticket| E[Jira API]
    E -->|Ticket Data| B
    B -->|Generate Summary| F[Gemini AI]
    F -->|AI Analysis| B
    E -->|Media Files| G[Media Processing]
    G -->|Processed Files| B
    B -->|Create Channel| H[Slack API]
    B -->|Post Messages| H
    B -->|Upload Files| H
    B -->|Update Ticket| E
    I[FireBot Commands] -->|summary/time/timeline/resolve| B
```

Key Components:
- **AWS Lambda**: Serverless function that processes Slack events and coordinates incident response
- **DynamoDB**: Provides distributed locking and deduplication across multiple Lambda instances
- **In-Memory Cache**: Fast local cache for event deduplication within a Lambda instance
- **Jira API**: Source of incident ticket data and media attachments
- **Gemini AI**: Generates intelligent summaries and analyzes incident details
- **Slack API**: Creates channels, posts messages, and handles file uploads
- **Media Processing**: Validates and processes images/videos from Jira tickets

## 📋 Usage

### Basic Workflow

1. **Trigger**: User mentions a Jira ticket in any Slack channel
   ```
   "We have an issue with ISD-12345 affecting the login system"
   ```

2. **Channel Creation**: FireBot creates `#incident-isd-12345-20250109-amc`

3. **AI Analysis**: Generates comprehensive summary and checks investigation checklist

4. **Media Processing**: Downloads and uploads any screenshots/videos from the ticket

5. **Creator Outreach**: Invites ticket creator and provides specific guidance on missing information

### Interactive Commands

Once in an incident channel, users can interact with FireBot:

```
firebot summary   # Generate comprehensive incident summary
firebot time      # Show incident duration
firebot timeline  # Generate detailed timeline with response metrics
firebot resolve   # Mark incident as resolved and post summary to Jira
```

### Sample Output

**Channel Creation Message:**
```
🚨 **Welcome to the incident channel for ISD-12345!**

🔗 **Jira Ticket:** https://yourcompany.atlassian.net/browse/ISD-12345

I'm FireBot, your AI-powered incident management assistant. Here's what I can help you with:

🤖 **AI Commands Available:**
• `firebot summary` - Generate a comprehensive AI summary of the incident
• `firebot time` - Show how long the incident has been open
• `firebot timeline` - Generate a detailed timeline of events and response metrics
• `firebot resolve` - Mark incident as resolved and post summary to Jira ticket
```

**Investigation Checklist Results:**
```
@john Thanks for reporting incident ISD-12345! A developer is on the way to help. 
To help our development team investigate more efficiently, could you please provide:

• Steps to reproduce - Clear step-by-step instructions to reproduce this issue
• Problem start time - When did this issue first start occurring?
• Practice-wide impact - Is this affecting all users or specific team members?

This information will help us resolve the issue faster. Thanks for your collaboration! 🐾
``` 