---
inclusion: always
---
# Incident response rules

These rules apply to every agent in this workspace.

- Base every statement on tool output. Cite the resource IDs, metric values and timestamps you observed.
- Use real resource IDs. Never invent IDs, ARNs or placeholders.
- Agents never change AWS resources. The workload is managed with GitOps: its infrastructure (CDK) and application
  code live in a CodeCommit repository, and a pipeline deploys every change merged into main.
- A fix is a pull request to that repository, opened only by the fixer agent. A person reviews and merges it, and the
  pipeline deploys it. Never suggest running AWS CLI write commands or console changes as the fix.
- Write user-facing text in Korean. Keep AWS product, service and API names in English (CloudWatch, Security Group, ALB, EC2, RDS).
- CloudTrail events can take 5 to 15 minutes to appear in LookupEvents. When the alarm is recent and no change
  event explains the symptom, say that the change actor is not yet visible in CloudTrail. Do not attribute the
  incident to an earlier change unless its timing and effect match the symptom.
