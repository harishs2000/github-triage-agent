import os
import time
from functools import lru_cache

from github import Auth, Github, GithubException, RateLimitExceededException, UnknownObjectException
from github.IssueComment import IssueComment

from config import REPO_FULL_NAME

MAX_ATTEMPTS = 4
BASE_BACKOFF_SECONDS = 2


class ToolError(Exception):
    """A tool call failed in a way the agent should see as a text result, not a crash."""


@lru_cache(maxsize=1)
def _get_client():
    token = os.environ["GITHUB_TOKEN"]
    return Github(auth=Auth.Token(token))


@lru_cache(maxsize=1)
def _get_repo():
    return _get_client().get_repo(REPO_FULL_NAME)


def _retry_after_seconds(exc):
    headers = getattr(exc, "headers", None) or {}
    value = headers.get("Retry-After") or headers.get("retry-after")
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _is_rate_limit(exc):
    if isinstance(exc, RateLimitExceededException):
        return True
    if isinstance(exc, GithubException) and exc.status == 403:
        message = str(exc.data.get("message", "")) if isinstance(exc.data, dict) else str(exc.data)
        return "rate limit" in message.lower()
    return False


def _call(operation_name, func):
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return func()
        except UnknownObjectException as exc:
            raise ToolError(f"{operation_name} failed: not found (404)") from exc
        except GithubException as exc:
            last_error = exc
            if _is_rate_limit(exc):
                if attempt == MAX_ATTEMPTS:
                    break
                wait = _retry_after_seconds(exc) or BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                time.sleep(wait)
                continue
            if exc.status == 403:
                raise ToolError(f"{operation_name} failed: permission denied") from exc
            if 500 <= exc.status < 600:
                if attempt == MAX_ATTEMPTS:
                    break
                time.sleep(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                continue
            raise ToolError(f"{operation_name} failed: {exc.status} {exc.data}") from exc
    raise ToolError(f"{operation_name} failed after {MAX_ATTEMPTS} attempts: {last_error}")


def _issue_to_dict(issue):
    return {
        "number": issue.number,
        "title": issue.title,
        "body": issue.body or "",
        "state": issue.state,
        "labels": [label.name for label in issue.labels],
        "assignees": [user.login for user in issue.assignees],
        "comments": issue.comments,
        "url": issue.html_url,
    }


def list_issues(state="open"):
    def op():
        repo = _get_repo()
        return [
            _issue_to_dict(issue)
            for issue in repo.get_issues(state=state)
            if issue.pull_request is None
        ]

    return _call("list_issues", op)


def get_issue(issue_number):
    def op():
        repo = _get_repo()
        return _issue_to_dict(repo.get_issue(issue_number))

    return _call("get_issue", op)


def search_issues(query):
    def op():
        full_query = f"repo:{REPO_FULL_NAME} is:issue {query}"
        return [_issue_to_dict(issue) for issue in _get_client().search_issues(full_query)]

    return _call("search_issues", op)


def add_labels(issue_number, labels):
    def op():
        repo = _get_repo()
        issue = repo.get_issue(issue_number)
        issue.add_to_labels(*labels)
        return {"number": issue_number, "labels": [label.name for label in issue.get_labels()]}

    return _call("add_labels", op)


def post_comment(issue_number, text):
    def op():
        repo = _get_repo()
        issue = repo.get_issue(issue_number)
        comment = issue.create_comment(text)
        return {"issue_number": issue_number, "comment_id": comment.id, "url": comment.html_url}

    return _call("post_comment", op)


def assign_issue(issue_number, username):
    def op():
        repo = _get_repo()
        issue = repo.get_issue(issue_number)
        issue.add_to_assignees(username)
        return {"number": issue_number, "assignees": [user.login for user in issue.assignees]}

    return _call("assign_issue", op)


def close_issue(issue_number):
    def op():
        repo = _get_repo()
        issue = repo.get_issue(issue_number)
        issue.edit(state="closed")
        return {"number": issue_number, "state": "closed"}

    return _call("close_issue", op)


def delete_comment(comment_id):
    def op():
        repo = _get_repo()
        comment = IssueComment(repo._requester, url=f"{repo.url}/issues/comments/{comment_id}")
        comment.delete()
        return {"comment_id": comment_id, "deleted": True}

    return _call("delete_comment", op)
