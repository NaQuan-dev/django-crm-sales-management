# Migration Plan Template

> Template only. Do not import real customer data until credentials, backups, mappings, and access rules have been reviewed.

## Phase 1: Empty-System Trial

Goals:

- The CRM can start successfully.
- Admin users can log in.
- Sales users and roles can be created.
- Customer creation, follow-up logs, reminders, and public-pool rules work on sample data.

Do not import real customer data in this phase.

## Phase 2: Small Pilot

Pilot with 1-2 sales users for one week:

- Add new inquiries manually.
- Add a follow-up after every customer contact.
- Check whether next-follow-up timing matches the team's workflow.
- Check whether public-pool reminders are too strict or too loose.

## Phase 3: Historical Data Import

Before importing historical data:

1. Export a backup from the source system.
2. Take a database snapshot of the target CRM.
3. Import 20 sample rows first and verify mappings.

Suggested mapping:

| Source field | CRM field |
| --- | --- |
| Customer ID | customer_no |
| Customer name | name |
| Contact person | contact_name |
| Phone | phone |
| WeChat | wechat |
| Email | email |
| Region | region |
| Source channel | source_channel |
| Customer type | customer_type |
| Demand | demand |
| Owner | owner |
| Customer level | grade |
| Customer status | status |
| Last contact time | last_contact_at |
| Next contact time | next_contact_at |

## Phase 4: Entry Switch

Keep the old source system as a read-only backup. New work should be entered into the CRM after the migration is verified.

## Phase 5: Automation

After the CRM is stable, add integrations such as chat reminders, conversational intake, voice-to-text follow-up creation, local AI tags, and customer-level suggestions.