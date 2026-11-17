"""Index-only To Archive backend. No filesystem, network or application services.

ArchiveTracker accepts an existing IndexStore, never opens a database itself.
Call from a worker with its own store. `availability` is an optional snapshot:
{source_id: {connected: bool, root: absolute Windows path}}. Supply a drive root
only when its identity is currently verified. No OS paths are probed here.

list_anime uses keyset pagination (after_path); details are loaded separately.
Limits are explicit and incomplete results never establish absence of episodes.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
import ntpath
import re
import unicodedata


VIDEO_EXTENSIONS = frozenset({'.mkv', '.mp4', '.avi', '.webm', '.m4v', '.mov', '.wmv', '.ts', '.m2ts', '.mpg', '.mpeg', '.ogm'})
ARCHIVE_EXTENSIONS = frozenset({'.rar', '.zip', '.7z'})


def title_key(value: str) -> str:
    """No punctuation, sequel, numeral or abbreviation removal."""
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def path_key(value: str) -> str:
    return ntpath.normpath(value).casefold()


def roots_overlap(left: str | None, right: str | None) -> bool | None:
    """Lexical Windows overlap only; cannot resolve junctions or offline mounts."""
    if not left or not right or not ntpath.isabs(left) or not ntpath.isabs(right):
        return None
    left, right = path_key(left), path_key(right)
    try:
        return ntpath.commonpath([left, right]) in (left, right)
    except ValueError:
        return False


@dataclass(frozen=True)
class Episode:
    identifier: str | None
    reason: str = ''
    season: int | None = None


def _number(value: str) -> str:
    whole, dot, fraction = value.partition('.')
    fraction = fraction.rstrip('0')
    return str(int(whole)) + ('.' + fraction if dot and fraction else '')


def parse_episode(filename: str) -> Episode:
    """Recognize a bounded whitelist of patterns; retain a reason for everything else.

    Release titles must use ` - NN`; only bracketed trailing metadata and vN are
    accepted. Arbitrary numbers in a title are never used as episode identifiers.
    """
    if len(filename) > 1024:
        return Episode(None, 'filename_too_long')
    stem, extension = ntpath.splitext(filename)
    if extension.casefold() not in VIDEO_EXTENSIONS | ARCHIVE_EXTENSIONS:
        return Episode(None, 'unsupported_file')
    if re.search(r'(?i)(?:[._ -]part\d+|[._ -]r\d{2})$', stem):
        return Episode(None, 'multipart_archive')
    if re.search(r'(?i)\b(?:ova|oad|special|sp|ncop|nced|batch|complete)\b', stem):
        return Episode(None, 'special_or_pack')
    if re.search(r'[\[(][^\])]*\d\s*(?:-|~|–|&|\+)\s*\d[^\])]*[\])]', stem):
        return Episode(None, 'range_or_multiple')
    for metadata in re.findall(r'\[([^\[\]]*)\]|\(([^()]*)\)', stem):
        value = ''.join(metadata).strip()
        if re.fullmatch(r'\d+(?:\.\d+)?', value) or re.search(r'(?i)\b(?:s\d+(?:e\d+)?|ep\s*\d+|episode\s*\d+)\b', value):
            return Episode(None, 'episode_like_metadata')
    # Strip balanced bracket groups, retaining their content only as metadata.
    clean = re.sub(r'\[[^\[\]]*\]|\([^()]*\)', '', stem).strip()
    if any(c in clean for c in '[]()'):
        return Episode(None, 'unbalanced_metadata')
    if re.fullmatch(r'\d+(?:\.\d+)?\s*(?:-|~|–|&|\+)\s*\d+(?:\.\d+)?', clean):
        return Episode(None, 'range_or_multiple')
    number = r'(\d{1,4}(?:\.\d{1,3})?)'
    suffix = r'(?:v[1-9]\d*)?'
    match = re.fullmatch(number + suffix, clean, re.I)
    if match:
        if int(match[1].split('.')[0]) in {480, 720, 1080, 2160, 4320} or int(match[1].split('.')[0]) >= 1900:
            return Episode(None, 'resolution_or_year')
        return Episode(_number(match[1]))
    match = re.fullmatch(r'(?:.*?\s+)?S(\d{1,2})E' + number + suffix, clean, re.I)
    if match:
        return Episode(_number(match[2]), season=int(match[1]))
    # Requiring a nonnumeric prefix avoids interpreting a bare year/range.
    match = re.fullmatch(r'(?:.*?\s+)?(?:episode|ep|e)\s*' + number + suffix, clean, re.I)
    release_pattern = match is None
    if not match:
        match = re.fullmatch(r'(?=.*[^\W\d_]).+?\s+-\s+' + number + suffix, clean, re.I)
    if match:
        if release_pattern and int(match[1].split('.')[0]) in {480, 720, 1080, 2160, 4320}:
            return Episode(None, 'resolution_or_episode')
        if re.search(r'(?i)(?:\bS\d+E\d+|\b(?:episode|ep)\s*\d+|\s-\s*\d+)', clean[:match.start(1)]):
            return Episode(None, 'multiple_episode_markers')
        return Episode(_number(match[1]), season=season_number(clean[:match.start(1)]))
    return Episode(None, 'ambiguous_filename')


def season_number(title: str) -> int | None:
    markers = re.findall(r'(?i)\b(?:season\s*|s)(\d+)\b', title)
    return int(markers[0]) if len(markers) == 1 else None


def compare_episodes(temporary: list[str], destination: list[str], *,
                     temporary_title: str = '', destination_title: str = '') -> dict:
    """Distinct identifier sets and all original names; no media equality claim."""
    def collect(names, title):
        groups, review = {}, []
        season = season_number(title)
        for name in names:
            result = parse_episode(name)
            reason = result.reason
            if result.season is not None and result.season != season:
                reason = 'season_context_conflict_or_unknown'
            if reason:
                review.append({'filename': name, 'reason': reason})
            else:
                groups.setdefault(result.identifier, []).append(name)
        return groups, review
    temp, temp_unknown = collect(temporary, temporary_title)
    archived, archived_unknown = collect(destination, destination_title)
    left, right = set(temp), set(archived)
    # Numeric ordering without float conversion.
    def order(value):
        a, _, b = value.partition('.')
        return int(a), b.ljust(3, '0')
    return {'archived': sorted(right, key=order), 'pending': sorted(left-right, key=order),
            'both': sorted(left & right, key=order), 'temporary_files': temp,
            'destination_files': archived,
            'counts': {'archived': len(right), 'pending': len(left-right), 'both': len(left & right),
                       'unknown': len(temp_unknown) + len(archived_unknown)},
            'unknown': {'temporary': temp_unknown, 'destination': archived_unknown}}


def _snapshot(method):
    """Keep reads consistent and writes atomic, including nested calls."""
    @wraps(method)
    def read(self, *args, **kwargs):
        self.conn.execute('SAVEPOINT archive_tracking_read')
        try:
            result = method(self, *args, **kwargs)
        except BaseException:
            self.conn.execute('ROLLBACK TO archive_tracking_read')
            self.conn.execute('RELEASE archive_tracking_read')
            raise
        else:
            self.conn.execute('RELEASE archive_tracking_read')
            return result
    return read


class ArchiveTracker:
    """Public facade: designate, confirm, unlink, list_anime, compare.

    list_anime returns folders without resolving candidates (lazy `match`). All
    association mutations validate registered sources and existing indexed folders.
    Confirmations may override title differences, never self/known-overlap safety.
    """
    def __init__(self, store, availability: dict | None = None):
        self.store = store
        self.conn = store.conn
        self.availability = availability or {}
        self.conn.create_function('archive_title_key', 1, title_key, deterministic=True)
        self.conn.create_function('archive_path_key', 1, path_key, deterministic=True)

    def sources(self) -> dict:
        rows = self.conn.execute(
            "SELECT d.id,COALESCE(p.custom_label,s.label,d.label),d.last_indexed,s.path,d.letter "
            "FROM drives d LEFT JOIN folder_sources s ON s.id=d.id "
            "LEFT JOIN source_preferences p ON p.source_id=d.id "
            "WHERE COALESCE(p.excluded,0)=0 AND (substr(d.id,1,7)<>'folder:' OR s.id IS NOT NULL)")
        return {sid: {'source_id': sid, 'label': label, 'last_indexed': stamp,
                      'connected': self.availability.get(sid, {}).get('connected'),
                      'root': root or self.availability.get(sid, {}).get('root'),
                      'last_known_location': root or letter, 'potentially_stale': True}
                for sid, label, stamp, root, letter in rows}

    def active_source(self) -> str | None:
        return self.store.archive_source_id()

    @_snapshot
    def designate(self, source_id: str | None) -> None:
        if source_id is not None and (not source_id.startswith('folder:') or source_id not in self.sources()):
            raise ValueError('Choose a registered, indexed manual source.')
        self.conn.execute('DELETE FROM archive_tracking_settings')
        if source_id is not None:
            self.conn.execute('INSERT INTO archive_tracking_settings VALUES(1,?)', (source_id,))

    @staticmethod
    def _limit(limit, maximum):
        if type(limit) is not int or not 1 <= limit <= maximum:
            raise ValueError(f'Limit must be between 1 and {maximum}.')

    @_snapshot
    def list_anime(self, *, after_path: str = '', limit: int = 100, term: str = '') -> dict:
        self._limit(limit, 200)
        sid = self.active_source()
        if sid is None or sid not in self.sources():
            return {'source_id': sid, 'items': [], 'next_after': None}
        rows = self.conn.execute(
            "SELECT path,name FROM entries WHERE drive_id=? AND parent='' AND is_folder=1 "
            "AND path>? AND instr(archive_title_key(name),?)>0 ORDER BY path LIMIT ?",
            (sid, after_path, title_key(term), limit+1)).fetchall()
        return {'source_id': sid, 'items': [{'path': p, 'name': n} for p, n in rows[:limit]],
                'next_after': rows[limit-1][0] if len(rows) > limit else None}

    def _folder(self, sid, path):
        key = self.store.manual_folder_path(path)
        rows = self.conn.execute(
            'SELECT path,name,parent FROM entries WHERE drive_id=? AND is_folder=1 '
            'AND archive_path_key(path)=? LIMIT 2', (sid, key)).fetchall()
        return rows[0] if len(rows) == 1 else None

    @_snapshot
    def search_destinations(self, term: str, *, limit: int = 50) -> dict:
        """Bounded manual picker; confirmation still revalidates identity and overlap."""
        self._limit(limit, 200)
        sid = self.active_source()
        sources = self.sources()
        if sid not in sources:
            raise ValueError('No active temporary source.')
        rows = self.conn.execute(
            'SELECT e.drive_id,e.path,e.name FROM entries e JOIN drives d ON d.id=e.drive_id '
            'LEFT JOIN folder_sources s ON s.id=d.id LEFT JOIN source_preferences p ON p.source_id=d.id '
            "WHERE e.is_folder=1 AND e.drive_id<>? AND COALESCE(p.excluded,0)=0 "
            "AND (substr(d.id,1,7)<>'folder:' OR s.id IS NOT NULL) AND instr(archive_title_key(e.name),?)>0 "
            'ORDER BY e.name,e.drive_id,e.path LIMIT ?', (sid, title_key(term), limit+1)).fetchall()
        return {'items': [dict(sources[dest], path=path, name=name,
                               overlap=self._overlap(sid, dest, sources)) for dest, path, name in rows[:limit]],
                'truncated': len(rows) > limit}

    def _temporary(self, path):
        sid = self.active_source()
        if sid not in self.sources():
            raise ValueError('No active indexed temporary source.')
        folder = self._folder(sid, path)
        if not folder or folder[2] != '':
            raise ValueError('Choose an immediate indexed anime folder.')
        return sid, folder

    def _overlap(self, temporary_source, destination_source, sources):
        if temporary_source == destination_source:
            return True
        return roots_overlap(sources[temporary_source]['root'], sources[destination_source]['root'])

    @_snapshot
    def confirm(self, path: str, destination_source: str, destination_path: str) -> None:
        sid, folder = self._temporary(path)
        sources = self.sources()
        if destination_source not in sources or not self._folder(destination_source, destination_path):
            raise ValueError('Destination is not an eligible indexed folder.')
        if self._overlap(sid, destination_source, sources) is True:
            raise ValueError('Self-comparison or overlapping roots are not allowed.')
        self.conn.execute(
            'INSERT INTO archive_folder_links(source_id,normalized_path,destination_source_id,destination_path) '
            "VALUES(?,?,?,?) ON CONFLICT(source_id,normalized_path) DO UPDATE SET "
            "destination_source_id=excluded.destination_source_id,destination_path=excluded.destination_path,confirmed_at=datetime('now')",
            (sid, path_key(folder[0]), destination_source, self.store.manual_folder_path(destination_path)))

    @_snapshot
    def unlink(self, path: str) -> None:
        sid = self.active_source()
        self.conn.execute('DELETE FROM archive_folder_links WHERE source_id=? AND normalized_path=?',
                          (sid, self.store.manual_folder_path(path)))

    @_snapshot
    def match(self, path: str, *, limit: int = 50) -> dict:
        self._limit(limit, 200)
        sid, folder = self._temporary(path)
        sources = self.sources()
        link = self.conn.execute('SELECT destination_source_id,destination_path FROM archive_folder_links '
                                 'WHERE source_id=? AND normalized_path=?', (sid, path_key(path))).fetchone()
        result = {'state': 'no_match', 'temporary': dict(sources[sid], path=folder[0], name=folder[1]),
                  'destination': None, 'candidates': [], 'truncated': False}
        if link:
            dest = self._folder(*link) if link[0] in sources else None
            if not dest:
                result.update(state='invalid_destination', previous_destination=link)
            elif self._overlap(sid, link[0], sources) is True:
                result['state'] = 'overlap_review'
            else:
                result.update(state='confirmed', destination=dict(sources[link[0]], path=dest[0], name=dest[1]),
                              overlap_unverified=self._overlap(sid, link[0], sources) is None)
            return result
        # One streaming SQL scan of indexed folder names, never recursive disk I/O.
        rows = self.conn.execute(
            'SELECT e.drive_id,e.path,e.name FROM entries e JOIN drives d ON d.id=e.drive_id '
            'LEFT JOIN folder_sources s ON s.id=d.id LEFT JOIN source_preferences p ON p.source_id=d.id '
            "WHERE e.is_folder=1 AND e.drive_id<>? AND COALESCE(p.excluded,0)=0 "
            "AND (substr(d.id,1,7)<>'folder:' OR s.id IS NOT NULL) AND archive_title_key(e.name)=? "
            'ORDER BY e.drive_id,e.path LIMIT ?', (sid, title_key(folder[1]), limit+1)).fetchall()
        result['truncated'] = len(rows) > limit
        for dest_sid, dest_path, name in rows[:limit]:
            overlap = self._overlap(sid, dest_sid, sources)
            result['candidates'].append(dict(sources[dest_sid], path=dest_path, name=name,
                                              overlap=overlap))
        if result['truncated']:
            result['state'] = 'multiple_candidates'
        elif any(c['overlap'] is True for c in result['candidates']):
            result['state'] = 'overlap_review'
        elif len(rows) > 1:
            result['state'] = 'multiple_candidates'
        elif rows:
            result['state'] = 'suggested'
        return result

    def _files(self, source, path, limit):
        rows = self.conn.execute('SELECT name,is_folder FROM entries WHERE drive_id=? AND parent=? '
                                 'ORDER BY path LIMIT ?', (source, path, limit+1)).fetchall()
        return [n for n, folder in rows[:limit] if not folder], any(f for _, f in rows), len(rows) > limit

    @_snapshot
    def compare(self, path: str, *, file_limit: int = 5000) -> dict:
        self._limit(file_limit, 20000)
        result = self.match(path)
        result['comparison'] = None
        if result['state'] != 'confirmed':
            return result
        temp, dest = result['temporary'], result['destination']
        left, left_nested, left_truncated = self._files(temp['source_id'], temp['path'], file_limit)
        right, right_nested, right_truncated = self._files(dest['source_id'], dest['path'], file_limit)
        result['nested_content_review'] = {'temporary': left_nested, 'destination': right_nested}
        result['files_truncated'] = left_truncated or right_truncated
        if result['files_truncated']:
            result['comparison_state'] = 'incomplete_review'
            return result
        seasons = season_number(temp['name']), season_number(dest['name'])
        if all(s is not None for s in seasons) and seasons[0] != seasons[1]:
            result['comparison_state'] = 'season_conflict_review'
            return result
        result['comparison'] = compare_episodes(left, right, temporary_title=temp['name'], destination_title=dest['name'])
        result['comparison_state'] = 'review' if left_nested or right_nested or any(result['comparison']['unknown'].values()) else 'compared'
        return result
