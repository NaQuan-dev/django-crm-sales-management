# Business Rules Template

> Template only. Adjust the rules for your real sales process before production use.

## Customer Tags

The default version uses local rule-based tagging and does not call external AI services. Tags can be generated from source, demand, notes, and contact quality. Example tags include:

- Short-video lead
- Website inquiry
- Exhibition lead
- Referral
- Equipment inquiry
- Price sensitive
- Near-term intent
- Overseas customer
- Missing contact details
- Possibly invalid

If the deployment environment supports it, this can later be upgraded to a local model such as Ollama.

## Follow-up Logs

Each customer can have multiple follow-up logs. Core fields include contact time, channel, summary, result, next action, and next follow-up time.

If the next follow-up time is empty, the system can suggest one based on the customer level.

## Next Follow-up Defaults

| Customer level | Suggested next contact |
| --- | --- |
| Key account | 3 days |
| Interested | 7 days |
| Normal | 14 days |
| Potential | 21 days |
| Nurture | 30 days |
| Unknown | 14 days |
| Closed-won | 30 days |
| Invalid | No reminder |

## Public Pool Rules

Default template rule:

1. Customers without follow-up activity for 30 days generate a reminder for the current owner.
2. If there is still no new follow-up after the grace period, the customer enters the public pool.
3. Public-pool customers keep their historical records and can be claimed or reassigned.

Configurable environment variables:

```env
CRM_PUBLIC_POOL_DAYS=30
CRM_PUBLIC_POOL_GRACE_HOURS=24
```

## Roles

- Administrator: system maintenance and full access.
- Sales leader: view all records, assign owners, review workload and public-pool status.
- Salesperson: manage owned/co-owned customers, create follow-ups, quotes, and reminders.
- Finance: contract/payment views and payment confirmation.
- Technician: sample/testing and visit-related work where enabled.