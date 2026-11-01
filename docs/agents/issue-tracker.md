# Issue tracker: GitHub

Issues and specs live at https://github.com/Jonathan0823/study-habit-engine.
Use the `gh` CLI. Infer the repository from the origin remote.

## Conventions

- Create: `gh issue create --title "..." --body "..."`.
  Use a heredoc or `--body-file` for multiline bodies.
- Read: `gh issue view <number> --json number,title,body,labels,comments,assignees`.
- List: `gh issue list --state open --json number,title,body,labels,assignees`.
  Apply label and state filters as needed; paginate when necessary.
- Comment: `gh issue comment <number> --body "..."`.
- Labels: `gh issue edit <number> --add-label "..."` or `--remove-label "..."`.
- Close: `gh issue close <number>`.

When asked to publish to the tracker, create a GitHub issue.
When asked to fetch a ticket, read its GitHub issue.

GitHub issues and pull requests share a number space. When ambiguous,
try `gh pr view <number>` and fall back to `gh issue view <number>`.

## Pull requests as a triage surface

PRs as a request surface: no.

## Wayfinding operations

- Map: one issue labelled `wayfinder:map`.
- Tickets: child issues carrying only their `wayfinder:<type>` label:
  research, prototype, grilling, or task.
- Link children using native sub-issues:
  `gh api --method POST repos/{owner}/{repo}/issues/<parent>/sub_issues -F sub_issue_id=<child-db-id>`.
  Obtain database IDs with
  `gh api repos/{owner}/{repo}/issues/<number> --jq .id`.
- If sub-issues are unavailable, use a task list in the map and
  `Part of #<map>` in each child.
- Blocking: use native dependencies:
  `gh api --method POST repos/{owner}/{repo}/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`.
  Only if unavailable, use `Blocked by: #<number>` in ticket bodies.
- Frontier: enumerate the map's children, retaining open, unassigned
  tickets with no open blockers. Use creation order, oldest first.
  Native `issue_dependencies_summary.blocked_by` reports open blockers;
  for body-based dependencies, check each blocker's state.
- Claim before working: `gh issue edit <number> --add-assignee @me`.
- Resolve: post a resolution comment, close the ticket, then append
  a named link and one-line gist to the map's Decisions so far.
- Refer to maps and tickets by linked title, not bare issue number.
