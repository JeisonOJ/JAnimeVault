"""Read-only global ordering for To Archive; comparison rules stay in the backend."""
from indexer.archive_tracking import title_key


def summarize(result):
    counts = (result.get('comparison') or {}).get('counts')
    review = result.get('comparison_state') != 'compared' or result.get('overlap_unverified', False)
    return counts, review


def priority(result):
    if result['state'] != 'confirmed':
        return 0  # Destination assignment/repair is needed, not zero pending.
    counts, review = summarize(result)
    if counts is not None and counts['pending'] > 0:
        return 1
    if review or counts is None:
        return 2
    return 3


def overview_page(tracker, offset=0, term='', limit=25):
    """Rank the entire temporary source before slicing, not each page separately.

    Keep only compact sort keys for all anime, never all filename detail trees.
    A single SQLite snapshot prevents inconsistent rankings during re-indexing.
    """
    offset = max(0, int(offset or 0))
    conn = tracker.conn
    conn.execute('SAVEPOINT archive_presentation')
    try:
        ranked, after = [], ''
        while True:
            batch = tracker.list_anime(after_path=after, limit=200, term=term)
            for folder in batch['items']:
                result = tracker.compare(folder['path'])
                ranked.append((priority(result), title_key(folder['name']), folder['path']))
            after = batch['next_after']
            if after is None:
                break
        ranked.sort()
        offset = min(offset, ((len(ranked)-1)//limit)*limit if ranked else 0)
        rows = []
        for group, _, path in ranked[offset:offset+limit]:
            result = tracker.compare(path)
            result['counts'], result['review'] = summarize(result)
            result.pop('comparison', None)
            result['priority'] = group
            rows.append(result)
        return {'items': rows, 'offset': offset, 'total': len(ranked),
                'nextAfter': str(offset+limit) if offset+limit < len(ranked) else ''}
    finally:
        conn.execute('RELEASE archive_presentation')
