# Contributing

Thanks for your interest in improving this project. The guide below keeps the repository consistent and reviewable.

## Branching

- `main` is protected and always deployable.
- Work happens on topic branches: `feat/<short-slug>`, `fix/<short-slug>`, `docs/<short-slug>`, `chore/<short-slug>`.
- Keep branches short-lived. Rebase onto `main` before opening a PR.

## Commit Style

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short imperative summary>

<optional body — wrap at 72 chars>

<optional footer — e.g. Closes #42>
```

Allowed types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `perf`, `ci`.

Examples:

```
feat(processor): add DynamoDB idempotency check on eventId
fix(reader): handle empty leaderboard with 200 + empty array
docs(runbook): document DLQ redrive procedure
```

## Pull Request Checklist

Before marking a PR ready for review, confirm:

- [ ] The change is focused. One logical change per PR.
- [ ] Commit messages follow the style above.
- [ ] Documentation is updated: `README.md`, `docs/ARCHITECTURE.md`, ADRs, or `docs/RUNBOOK.md` as applicable.
- [ ] No secrets, account IDs, or personal AWS resources are committed.
- [ ] If the change affects infrastructure, `cdk diff` output is attached to the PR description.
- [ ] Tests (where applicable) pass locally.
- [ ] The PR description explains the *why*, not just the *what*.

## Documentation Updates

Documentation is part of the deliverable, not a follow-up task. The rule of thumb:

- New feature → update `README.md` Features section and the relevant doc under `docs/`.
- Architecture change → update `docs/ARCHITECTURE.md` and add a new ADR under `docs/adr/` explaining the decision.
- Operational change → update `docs/RUNBOOK.md` before the PR merges.

## Code Review

Reviewers look for:

1. Correctness and safety of the change.
2. Alignment with the architectural intent documented in ADRs.
3. Clear, minimal code with meaningful names.
4. Updated documentation.
5. No new secrets or hard-coded values that should be configuration.

Responding to review: address every comment with either a code change or a reply explaining why you disagree. Don't resolve threads silently.

## Reporting Issues

Issues are welcome. A good bug report includes:

- What you expected to happen.
- What actually happened.
- Minimal steps to reproduce.
- AWS region, tool versions, any relevant logs (with secrets redacted).
