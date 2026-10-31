"""Publish aggregate GitLab activity only. No third-party Python dependencies.

Commits: exact configured author email, unique SHA across selected repositories,
non-merge commits, dated by committed_date. MRs: authored by token owner, dated
by merged_at. This is repository activity, not a time sheet or productivity score.
"""
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from html import escape
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

TZ = ZoneInfo('America/Toronto')


class SetupError(Exception):
    pass


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A login/SSO redirect must never forward a token to another destination.
        return None


class GitLab:
    def __init__(self, base, token):
        base = base.strip().rstrip('/')
        parsed = urlsplit(base)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise SetupError('GITLAB_URL must be an HTTPS base URL without credentials or query parameters.')
        self.api = base + '/api/v4'
        self.token = token
        self.opener = build_opener(NoRedirects())

    def get(self, path, params=None):
        suffix = '?' + urlencode(params) if params else ''
        request = Request(self.api + path + suffix, headers={
            'PRIVATE-TOKEN': self.token, 'Accept': 'application/json',
            'User-Agent': 'aggregate-profile-chart/1.0'})
        try:
            with self.opener.open(request, timeout=40) as response:
                return json.load(response), response.headers.get('X-Next-Page', '')
        except HTTPError as error:
            # Never print response bodies, request URLs, or private resource names.
            raise SetupError(f'GitLab returned HTTP {error.code}. Check token scope, expiry, project access, and SSO/API settings.') from None
        except (URLError, TimeoutError, OSError) as error:
            reason = getattr(error, 'reason', error)
            raise SetupError(
                f'GitLab connection failed: {type(reason).__name__} '
                f'(errno={getattr(reason, "errno", None)}).'
            ) from None
        except (ValueError, UnicodeError):
            raise SetupError('GitLab did not return valid JSON. Check the base URL and any sign-in proxy.') from None

    def items(self, path, params=None):
        page = 1
        for _ in range(1000):
            data, next_page = self.get(path, dict(params or {}, per_page=100, page=page))
            if not isinstance(data, list):
                raise SetupError('Unexpected GitLab list response; existing chart was not replaced.')
            yield from data
            if next_page:
                try:
                    following = int(next_page)
                except ValueError:
                    raise SetupError('Invalid GitLab pagination response.') from None
                if following <= page:
                    raise SetupError('GitLab pagination did not advance.')
                page = following
            elif len(data) == 100:
                page += 1  # Commits API may omit some pagination headers.
            else:
                return
        raise SetupError('GitLab pagination limit reached; refusing to publish partial counts.')


def event_day(value):
    if not isinstance(value, str):
        raise SetupError('An activity record is missing its timestamp.')
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError
        return stamp.astimezone(TZ).date()
    except ValueError:
        raise SetupError('An activity timestamp could not be interpreted.') from None


def collect(api, projects, emails, now):
    end = now.astimezone(TZ).date()
    start = end - timedelta(days=364)
    since = datetime.combine(start, time.min, TZ).astimezone(timezone.utc).isoformat()
    until = now.astimezone(timezone.utc).isoformat()
    user, _ = api.get('/user')
    if not isinstance(user, dict) or not isinstance(user.get('id'), int):
        raise SetupError('Could not resolve the GitLab token owner.')
    commits, mrs = Counter(), Counter()
    seen_commits, seen_mrs = set(), set()
    for project in projects:
        for email in sorted(emails):
            for record in api.items(f'/projects/{project}/repository/commits', {
                    'all': 'true', 'author': email, 'since': since, 'until': until}):
                if record.get('author_email', '').strip().casefold() not in emails:
                    continue
                parents = record.get('parent_ids')
                if not isinstance(parents, list):
                    raise SetupError('Commit metadata is missing parent information.')
                if len(parents) > 1:
                    continue
                sha = record.get('id')
                if not sha:
                    raise SetupError('Commit metadata is missing its identifier.')
                day = event_day(record.get('committed_date'))
                if start <= day <= end and sha not in seen_commits:
                    seen_commits.add(sha)
                    commits[day] += 1
        for record in api.items(f'/projects/{project}/merge_requests', {
                'state': 'merged', 'scope': 'all', 'author_id': user['id'],
                'updated_after': since}):
            if record.get('state') != 'merged' or record.get('author', {}).get('id') != user['id']:
                continue
            if record.get('iid') is None:
                raise SetupError('Merge-request metadata is missing its identifier.')
            key = (project, record['iid'])
            day = event_day(record.get('merged_at'))
            if start <= day <= end and key not in seen_mrs:
                seen_mrs.add(key)
                mrs[day] += 1
    return start, end, commits, mrs


def render(start, end, commits, mrs):
    totals = commits + mrs
    first = start - timedelta(days=(start.weekday() + 1) % 7)
    columns = (end - first).days // 7 + 1
    width = max(820, 55 + columns * 14)
    cells, months = [], []
    previous_month = None
    palette = ['#ebedf0', '#ffdbb5', '#ffb36b', '#ef822e', '#b24a12']
    for offset in range((end - first).days + 1):
        day = first + timedelta(days=offset)
        if day < start:
            continue
        column, row = divmod(offset, 7)
        x, y = 38 + column * 14, 103 + row * 14
        n = totals[day]
        level = 0 if not n else 1 if n < 3 else 2 if n < 6 else 3 if n < 11 else 4
        detail = f'{day.isoformat()}: {commits[day]} commits; {mrs[day]} authored MRs merged'
        cells.append(f'<rect x="{x}" y="{y}" width="11" height="11" rx="2" fill="{palette[level]}"><title>{escape(detail)}</title></rect>')
        if day.month != previous_month and (day.day == 1 or day == start):
            # Suppress crowded month labels in a partial opening week.
            if not months or x - months[-1][0] >= 30:
                months.append((x, f'<text x="{x}" y="94">{day:%b}</text>'))
            previous_month = day.month
    count_commits, count_mrs = sum(commits.values()), sum(mrs.values())
    active = len(set(commits) | set(mrs))
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="286" viewBox="0 0 {width} 286" role="img" aria-labelledby="title desc">
<title id="title">GitLab work activity</title>
<desc id="desc">{count_commits} non-merge commits authored by the configured user, {count_mrs} of their merge requests merged, {active} days with recorded activity. Selected repositories; last 365 days. Counts are not hours or a productivity score.</desc>
<rect x="0.5" y="0.5" width="{width-1}" height="285" rx="10" fill="white" stroke="#d0d7de"/>
<style>text{{font:12px Arial,sans-serif;fill:#57606a}} .heading{{font-size:20px;font-weight:700;fill:#24292f}} .metric{{font-size:14px;fill:#24292f}}</style>
<text class="heading" x="22" y="31">GitLab work activity</text>
<text class="metric" x="22" y="58">{count_commits:,} authored commits · {count_mrs:,} authored MRs merged · {active} activity days</text>
{''.join(t for _, t in months)}
<text x="7" y="126">M</text><text x="7" y="154">W</text><text x="7" y="182">F</text>
{''.join(cells)}
<text x="22" y="224">{start.isoformat()} to {end.isoformat()} · Toronto dates · Updated {end.isoformat()}</text>
<text x="22" y="247">Cells = commits + authored MRs merged. Aggregate counts from selected repositories.</text>
<text x="22" y="269">Separate from GitHub contributions. Repository activity does not measure hours worked.</text>
</svg>'''


def main():
    required = ['GITLAB_URL', 'GITLAB_TOKEN', 'GITLAB_PROJECT_IDS', 'GITLAB_AUTHOR_EMAILS']
    if any(not os.environ.get(k, '').strip() for k in required):
        raise SetupError('Add all four required GitHub repository secrets before running the workflow.')
    projects = sorted(set(re.split(r'[,\s]+', os.environ['GITLAB_PROJECT_IDS'].strip())))
    if not all(p.isascii() and p.isdecimal() for p in projects):
        raise SetupError('GITLAB_PROJECT_IDS must contain numeric project IDs separated by commas or new lines.')
    emails = {s.casefold() for s in re.split(r'[,;\s]+', os.environ['GITLAB_AUTHOR_EMAILS'].strip()) if s}
    if not emails or any('@' not in email for email in emails):
        raise SetupError('GITLAB_AUTHOR_EMAILS must contain your exact Git commit author email addresses.')
    api = GitLab(os.environ['GITLAB_URL'], os.environ['GITLAB_TOKEN'].strip())
    result = collect(api, projects, emails, datetime.now(timezone.utc))
    svg = render(*result)
    output = Path('assets/gitlab-activity.svg')
    output.parent.mkdir(exist_ok=True)
    temporary = output.with_suffix('.svg.tmp')
    temporary.write_text(svg, encoding='utf-8')
    temporary.replace(output)
    print('Aggregate chart generated. No raw API data was saved.')
    if not sum(result[2].values()):
        print('No matching commits counted. If unexpected, check configured author emails and project selection.')


if __name__ == '__main__':
    try:
        main()
    except SetupError as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)
    except Exception:
        print('ERROR: Unexpected response or runtime failure. The chart was not published; no private response data is logged.', file=sys.stderr)
        sys.exit(1)
