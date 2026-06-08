"""GitHub child issue readiness and dependency selection.

This module keeps GitHub behind a tiny injected adapter seam so unit tests can
exercise selection without live GitHub access. The adapter may expose any of:

- ``list_child_issues(owner, repo, parent_number)``
- ``list_sub_issues(owner, repo, parent_number)``
- ``get_child_issues(owner, repo, parent_number)``

and should expose ``get_issue(owner, repo, number)`` for blocker lookups.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, cast

READY_LABEL = "ready-for-agent"
DONE_LABEL = "agent:done"

GITHUB_ISSUE_URL_RE = re.compile(
    r"https?://github\.com/"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/issues/(?P<number>[1-9][0-9]*)"
    r"(?:[#?][^\s)>]*)?(?=$|[\s)>\],.;:!])",
    re.IGNORECASE,
)
ISSUE_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_/.-])#(?P<number>[1-9][0-9]*)\b")
BLOCKED_BY_HEADING_RE = re.compile(r"^\s*##\s+Blocked by\s*$", re.IGNORECASE)
H2_HEADING_RE = re.compile(r"^\s*##\s+")


@dataclass(frozen=True, order=True)
class IssueKey:
    owner: str
    repo: str
    number: int

    @property
    def repository(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def ref(self) -> str:
        return f"{self.repository}#{self.number}"


@dataclass(frozen=True)
class GitHubIssue:
    owner: str
    repo: str
    number: int
    title: str = ""
    body: str = ""
    state: str = "open"
    labels: tuple[str, ...] = field(default_factory=tuple)

    @property
    def key(self) -> IssueKey:
        return IssueKey(self.owner, self.repo, self.number)

    @property
    def repository(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def ref(self) -> str:
        return f"{self.repository}#{self.number}"

    @property
    def is_closed(self) -> bool:
        return self.state.lower() == "closed"

    def has_label(self, label: str) -> bool:
        wanted = label.casefold()
        return any(existing.casefold() == wanted for existing in self.labels)

    @property
    def is_done(self) -> bool:
        return self.is_closed or self.has_label(DONE_LABEL)

    @property
    def is_ready(self) -> bool:
        return self.has_label(READY_LABEL)


@dataclass(frozen=True)
class ChildBlockers:
    child: GitHubIssue
    blockers: tuple[IssueKey, ...]
    unsatisfied: tuple[IssueKey, ...]

    @property
    def is_unblocked(self) -> bool:
        return not self.unsatisfied


@dataclass(frozen=True)
class ChildSelection:
    parent: IssueKey
    children: tuple[GitHubIssue, ...]
    selected: GitHubIssue | None
    blocked: tuple[ChildBlockers, ...]
    ineligible: tuple[GitHubIssue, ...]
    completed: tuple[GitHubIssue, ...]
    status: str
    message: str

    @property
    def has_runnable_child(self) -> bool:
        return self.selected is not None


def _label_name(label: Any) -> str:
    if isinstance(label, str):
        return label
    if isinstance(label, dict):
        return str(label.get("name", ""))
    return str(getattr(label, "name", ""))


def _labels(payload: Any) -> tuple[str, ...]:
    raw_labels = payload.get("labels", []) if isinstance(payload, dict) else getattr(payload, "labels", [])
    return tuple(name for label in raw_labels if (name := _label_name(label)))


def coerce_github_issue(default_owner: str, default_repo: str, payload: Any) -> GitHubIssue:
    if isinstance(payload, GitHubIssue):
        return payload
    if isinstance(payload, dict):
        owner = payload.get("owner", default_owner)
        repo = payload.get("repo", default_repo)
        number = payload.get("number")
        title = payload.get("title", "")
        body = payload.get("body", "") or ""
        state = payload.get("state", "open") or "open"
    else:
        owner = getattr(payload, "owner", default_owner)
        repo = getattr(payload, "repo", default_repo)
        number = getattr(payload, "number", None)
        title = getattr(payload, "title", "")
        body = getattr(payload, "body", "") or ""
        state = getattr(payload, "state", "open") or "open"
    if number is None:
        raise ValueError("GitHub issue payload did not include a number.")
    return GitHubIssue(
        owner=str(owner),
        repo=str(repo),
        number=int(number),
        title=str(title or ""),
        body=str(body or ""),
        state=str(state),
        labels=_labels(payload),
    )


def blocked_by_section(body: str) -> str:
    """Return the markdown content under ``## Blocked by`` until the next H2."""
    lines = (body or "").splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if BLOCKED_BY_HEADING_RE.match(line):
            start = index + 1
            break
    if start is None:
        return ""
    section: list[str] = []
    for line in lines[start:]:
        if H2_HEADING_RE.match(line):
            break
        section.append(line)
    return "\n".join(section)


def parse_blockers(body: str, default_owner: str, default_repo: str) -> tuple[IssueKey, ...]:
    """Parse issue-number and issue-link blockers from a child issue body."""
    section = blocked_by_section(body)
    if not section:
        return ()

    url_spans: list[tuple[int, int]] = []
    matches: list[tuple[int, IssueKey]] = []

    for match in GITHUB_ISSUE_URL_RE.finditer(section):
        url_spans.append(match.span())
        matches.append(
            (
                match.start(),
                IssueKey(match.group("owner"), match.group("repo"), int(match.group("number"))),
            )
        )

    def inside_url(position: int) -> bool:
        return any(start <= position < end for start, end in url_spans)

    for match in ISSUE_NUMBER_RE.finditer(section):
        if inside_url(match.start()):
            continue
        matches.append((match.start(), IssueKey(default_owner, default_repo, int(match.group("number")))))

    blockers: list[IssueKey] = []
    seen: set[IssueKey] = set()
    for _position, key in sorted(matches, key=lambda item: item[0]):
        if key not in seen:
            seen.add(key)
            blockers.append(key)
    return tuple(blockers)


def blocker_satisfied(issue: GitHubIssue) -> bool:
    """A blocker is satisfied when closed or labeled ``agent:done``."""
    return issue.is_done


def _list_child_payloads(github_client: Any, parent: IssueKey) -> Iterable[Any]:
    for name in ("list_child_issues", "list_sub_issues", "get_child_issues"):
        method = getattr(github_client, name, None)
        if callable(method):
            return cast(Iterable[Any], method(parent.owner, parent.repo, parent.number))
    raise TypeError(
        "github_client must expose list_child_issues(owner, repo, parent_number) "
        "or list_sub_issues/get_child_issues for child discovery"
    )


def _get_issue(github_client: Any, key: IssueKey) -> Any:
    getter = getattr(github_client, "get_issue", None)
    if callable(getter):
        return getter(key.owner, key.repo, key.number)
    if callable(github_client):
        return github_client(key.owner, key.repo, key.number)
    raise TypeError("github_client must expose get_issue(owner, repo, number) for blocker lookup")


def _format_issue(issue: GitHubIssue | IssueKey) -> str:
    title = getattr(issue, "title", "")
    suffix = f" {title}" if title else ""
    return f"{issue.repository}#{issue.number}{suffix}"


def _format_keys(keys: Iterable[IssueKey]) -> str:
    return ", ".join(key.ref for key in keys)


def select_next_child(parent: IssueKey, github_client: Any) -> ChildSelection:
    """Choose the first runnable child deterministically by issue number."""
    children = tuple(
        sorted(
            (coerce_github_issue(parent.owner, parent.repo, payload) for payload in _list_child_payloads(github_client, parent)),
            key=lambda issue: issue.number,
        )
    )

    ineligible = tuple(child for child in children if not child.is_ready)
    completed = tuple(child for child in children if child.is_ready and child.is_done)
    candidates = tuple(child for child in children if child.is_ready and not child.is_done)

    blocked: list[ChildBlockers] = []
    for child in candidates:
        blockers = parse_blockers(child.body, child.owner, child.repo)
        unsatisfied: list[IssueKey] = []
        for blocker in blockers:
            blocker_issue = coerce_github_issue(blocker.owner, blocker.repo, _get_issue(github_client, blocker))
            if not blocker_satisfied(blocker_issue):
                unsatisfied.append(blocker)
        child_blockers = ChildBlockers(child=child, blockers=blockers, unsatisfied=tuple(unsatisfied))
        if child_blockers.is_unblocked:
            return ChildSelection(
                parent=parent,
                children=children,
                selected=child,
                blocked=tuple(blocked),
                ineligible=ineligible,
                completed=completed,
                status="runnable",
                message=f"Next runnable child is #{child.number}: {child.title or child.ref}.",
            )
        blocked.append(child_blockers)

    if not children:
        status = "complete"
        message = f"Parent {parent.ref} has no GitHub child issues; there is no runnable child."
    elif candidates and blocked:
        status = "waiting_on_blockers"
        details = "; ".join(f"#{item.child.number} waits on {_format_keys(item.unsatisfied)}" for item in blocked)
        message = f"No child is currently runnable; waiting on blockers: {details}."
    elif completed and not candidates and not ineligible:
        status = "complete"
        message = f"Parent {parent.ref} is complete; all ready children are closed or labeled {DONE_LABEL}."
    elif ineligible and not candidates:
        status = "waiting_for_triage"
        message = f"No child is currently runnable; waiting for {READY_LABEL} on remaining child issues."
    else:
        status = "complete"
        message = f"Parent {parent.ref} is complete; there are no open ready children to run."

    return ChildSelection(
        parent=parent,
        children=children,
        selected=None,
        blocked=tuple(blocked),
        ineligible=ineligible,
        completed=completed,
        status=status,
        message=message,
    )
