# 🔥 FireBot - Intelligent Incident Management Bot

FireBot is an AWS Lambda-powered Slack bot designed to streamline incident management for veterinary software teams. When Jira incident tickets are mentioned in Slack, FireBot automatically creates dedicated incident channels, provides AI-powered analysis, uploads relevant media, and guides reporters through providing complete investigation details.

## ✨ Features

### 🤖 **Automated Incident Channel Management**
- **Smart Channel Creation**: Automatically creates incident channels when Jira tickets (ISD-XXXXX) are mentioned
- **Intelligent Naming**: Channels follow the pattern `incident-{ticket}-{date}-{hospital}`
- **User Invitation**: Automatically invites ticket reporters and mentioning users to incident channels
- **Deduplication**: Prevents duplicate processing and channel creation

### 🧠 **AI-Powered Ticket Analysis**
- **Gemini Integration**: Uses Google's Gemini AI to generate concise, actionable incident summaries
- **Intelligent Parsing**: Handles both plain text and Atlassian Document Format (ADF) descriptions
- **Fallback Models**: Automatic fallback between Gemini models for reliability

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
- **Size Validation**: Enforces file size limits and validates image integrity
- **Rich Context**: Uploads include author information and source ticket references

### 🎯 **Intelligent Creator Outreach**
- **User Lookup**: Finds ticket creators in Slack by email address
- **Structured Requests**: Provides specific, actionable requests for missing information
- **Encouraging Tone**: Uses supportive, collaborative messaging to reduce stress

### 🛡️ **Robust Error Handling**
- **Graceful Degradation**: Individual feature failures don't break core functionality
- **Comprehensive Logging**: Detailed logging for troubleshooting and monitoring
- **Duplicate Prevention**: Smart caching prevents duplicate processing
- **Bot Message Filtering**: Avoids processing its own messages

### 🤖 **Interactive Commands**
- **Channel Summary**: `firebot summary` - Generate comprehensive incident summaries using AI
- **Duration Tracking**: `firebot time` - Show how long the incident has been open
- **Smart Detection**: Only responds to commands in incident channels
- **AI-Powered Analysis**: Uses Gemini to analyze channel history and provide insights

## 🚀 Getting Started

### Prerequisites
- AWS Lambda environment
- Slack workspace with bot permissions
- Jira Cloud instance with API access
- Google Cloud project with Gemini API access

### Required Environment Variables

```bash
# Slack Configuration
SLACK_BOT_TOKEN=xoxb-your-slack-bot-token
SLACK_BOT_USER_ID=U1234567890  # Optional: helps prevent duplicate processing

# Jira Configuration  
JIRA_USERNAME=your-jira-email@company.com
JIRA_API_TOKEN=your-jira-api-token
JIRA_DOMAIN=yourcompany.atlassian.net
JIRA_HOSPITAL_FIELD=customfield_10297  # Hospital/practice field ID
JIRA_SUMMARY_FIELD=customfield_10250   # Summary field ID

# AI Configuration
GEMINI_API_KEY=your-gemini-api-key
GEMINI_MODEL=gemini-1.5-flash  # Optional: defaults to gemini-1.5-flash
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

### Dependencies

Install the required Python packages:

```bash
pip install -r requirements.txt
```

**requirements.txt:**
```
google-generativeai
requests
Pillow>=9.0.0
```

## 🏗️ Architecture

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Slack Event   │───▶│   AWS Lambda     │───▶│   Jira API      │
│   (Message)     │    │   (FireBot)      │    │   (Fetch Data)  │
└─────────────────┘    └──────────────────┘    └─────────────────┘
                              │
                              ▼
                       ┌──────────────────┐
                       │   Gemini AI      │
                       │   (Analysis)     │
                       └──────────────────┘
                              │
                              ▼
                       ┌──────────────────┐
                       │   Slack API      │
                       │   (Response)     │
                       └──────────────────┘
```

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
firebot summary  # Generate comprehensive incident summary
firebot time     # Show incident duration
```

### Sample Output

**Channel Creation Message:**
```
🚨 Incident channel #incident-isd-12345-20250109-amc has been created. 
Please move all communications there. 🚨
```

**AI Summary:**
```
Incident Summary:
PDF printing outage affecting all users and workstations. Started ~1 hour ago.

The system is experiencing a widespread printing issue where PDF generation 
fails across all practice locations. This appears to be a backend service 
disruption affecting core functionality.
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

**FireBot Commands Output:**

**Summary Command:**
```
📋 **Incident Summary**

Key Events:
• 14:30 - Issue first reported by @john
• 14:45 - @sarah joined to investigate
• 15:00 - Root cause identified as database connection issue
• 15:15 - Fix deployed to staging

Current Status: Monitoring fix in production
People Involved: @john (reporter), @sarah (developer), @mike (devops)
Next Steps: Verify fix resolves issue for all users
```

**Time Command:**
```
⏰ **Incident Duration**

This incident has been open for: **2 hours and 45 minutes**
Started: 2025-01-09 14:30:00 UTC
```

## 🔧 Configuration

### Custom Field Mapping

Update these environment variables to match your Jira instance:

```bash
JIRA_HOSPITAL_FIELD=customfield_10297  # Your hospital/practice field
JIRA_SUMMARY_FIELD=customfield_10250   # Your summary field  
```

### Investigation Checklist

The bot analyzes tickets against these items (configurable in code):
- Issue replication in customer's application
- Issue replication on Demo instance
- Steps to reproduce  
- Screenshots provided
- Problem start time
- Practice-wide impact
- Multi-practice impact

### File Upload Limits

- **Maximum file size**: 100MB per file
- **Supported formats**: All image/* and video/* MIME types
- **Validation**: Images are validated using PIL/Pillow
- **Upload method**: Modern Slack API (files.getUploadURLExternal + files.completeUploadExternal)

## 🐛 Troubleshooting

### Common Issues

**1. Duplicate Messages/Channels**
- Check `SLACK_BOT_USER_ID` is set correctly
- Verify bot message filtering is working
- Review event deduplication logs

**2. Media Upload Failures**
- Error `method_deprecated`: Using old Slack API (fixed in current version)
- Large files: Check file size limits in logs
- Invalid images: PIL validation may reject corrupted files

**3. Jira Authentication Issues**
- Verify `JIRA_API_TOKEN` is valid and has read permissions
- Check `JIRA_USERNAME` matches the token owner
- Confirm `JIRA_DOMAIN` format (without https://)

**4. AI Analysis Failures**
- Check `GEMINI_API_KEY` is valid
- Review quota limits in Google Cloud Console
- Bot falls back to simpler messages if AI fails

### Debugging

Enable detailed logging by checking CloudWatch Logs:

```python
print(f"Processing event: {event_id}")
print(f"Jira response: {response.status_code}")
print(f"AI model used: {model_name}")
```

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- **Slack API**: For robust messaging and file upload capabilities
- **Google Gemini**: For intelligent ticket analysis and natural language processing
- **Atlassian Jira**: For comprehensive incident tracking integration
- **AWS Lambda**: For serverless, scalable execution environment

## 📞 Support

For questions, issues, or feature requests:
1. Check the [troubleshooting section](#-troubleshooting)
2. Review CloudWatch logs for detailed error information
3. Open an issue in this repository

---

**Made with ❤️ for veterinary teams everywhere** 🐾 