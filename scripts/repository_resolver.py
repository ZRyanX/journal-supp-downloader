"""Bounded expansion of repository landing pages into downloadable artifacts."""
from urllib.parse import urlparse
import requests
from supp_finder import find_all_candidates, REPO_OR_COMPONENT_DOI_PATTERN, check_data_extension


def is_repository_landing(url):
    parsed = urlparse(url)
    if check_data_extension(parsed.path) or parsed.path.lower().endswith('.pdf'):
        return False
    host = (parsed.hostname or '').lower()
    return bool(REPO_OR_COMPONENT_DOI_PATTERN.search(url)) or (
        host in {'zenodo.org', 'figshare.com', 'osf.io', 'datadryad.org', 'data.mendeley.com'}
        and not any(part in parsed.path for part in ('/files/', '/download', '/ndownloader/'))
    )


def expand_repository_candidates(candidates, session=None):
    """Keep unresolved originals, preserve source pages, and prevent cyclic crawls."""
    owned = session is None
    http = session or requests.Session()
    visited = set()

    def expand(candidate, depth):
        if depth >= 2 or len(visited) >= 16 or candidate.url in visited or not is_repository_landing(candidate.url):
            return [candidate]
        visited.add(candidate.url)
        response = None
        try:
            response = http.get(candidate.url, timeout=20, stream=True,
                                headers={'Accept': 'text/html', 'Referer': candidate.source_url or candidate.url})
            response.raise_for_status()
            if 'html' not in response.headers.get('Content-Type', '').lower():
                return [candidate]
            body = bytearray()
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body) > 2 * 1024 * 1024:
                    raise ValueError('Repository page exceeds 2 MiB discovery limit')
            found = find_all_candidates(bytes(body).decode(response.encoding or 'utf-8', errors='replace'), response.url)
            children = [c for c in found.candidates if c.url not in visited]
            if not children:
                print(f'  [Repository] No file links resolved: {candidate.url}')
                return [candidate]
            result = []
            for child in children:
                child.extra_info['repository_parent'] = candidate.url
                result.extend(expand(child, depth + 1))
            parent = candidate.to_dict()
            parent['required_urls'] = [child.url for child in result]
            for child in result:
                child.extra_info.setdefault('parent_evidence', []).append(parent)
                child.extra_info['parent_evidence'].extend(candidate.extra_info.get('parent_evidence', []))
            return result
        except (requests.RequestException, ValueError) as exc:
            print(f'  [Repository] Unresolved {candidate.url}: {exc}')
            return [candidate]
        finally:
            if response is not None:
                response.close()
    try:
        result = {}
        for candidate in candidates:
            for resolved in expand(candidate, 0):
                if resolved.url in result:
                    result[resolved.url].extra_info.setdefault('parent_evidence', []).extend(resolved.extra_info.get('parent_evidence', []))
                else:
                    result[resolved.url] = resolved
        return list(result.values())
    finally:
        if owned:
            http.close()
