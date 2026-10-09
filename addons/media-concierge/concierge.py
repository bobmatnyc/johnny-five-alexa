#!/usr/bin/env python3
"""Shared media concierge. Credentials and durable jobs live outside the repo."""
import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET

HOME = Path.home() / '.local/share/media-concierge'


class MediaError(Exception):
    pass


def norm(value):
    return ''.join(x for x in unicodedata.normalize('NFKD', value).casefold() if x.isalnum())


def clean_movie(x, kind):
    return {'id': x.get('id'), 'external_id': x.get('tmdbId' if kind == 'movie' else 'tvdbId'), 'title': x.get('title'), 'year': x.get('year'), 'overview': (x.get('overview') or '')[:700], 'has_file': bool(x.get('hasFile')), 'seasons': [s['seasonNumber'] for s in x.get('seasons', []) if s.get('seasonNumber', 0) > 0]}


class Concierge:
    def __init__(self, home=HOME):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config = json.loads((self.home / 'config.json').read_text())
        self.dbpath = self.home / 'jobs.sqlite3'
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, identity TEXT UNIQUE, kind TEXT, external_id INTEGER, title TEXT, year INTEGER, seasons TEXT, arr_id INTEGER, phase TEXT, detail TEXT, created REAL, updated REAL, last_progress REAL, progress TEXT, scans INTEGER DEFAULT 0, last_scan REAL DEFAULT 0)')
        self.dbpath.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.dbpath, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def mutation_lock(self):
        with (self.home / 'workflow.lock').open('a') as stream:
            os.chmod(stream.name, 0o600)
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def api(self, app, path, method='GET', data=None):
        c = self.config[app]
        headers = {'Content-Type': 'application/json'}
        if app in ('radarr', 'sonarr'):
            headers['X-Api-Key'] = c['api_key']
        elif app == 'plex':
            headers['X-Plex-Token'] = c['token']
        elif app == 'nzbget':
            headers['Authorization'] = 'Basic '+base64.b64encode((c['username']+':'+c['password']).encode()).decode()
        req = urllib.request.Request(c['url']+path, None if data is None else json.dumps(data).encode(), headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                raw = response.read(100_000_001)
                if len(raw) > 100_000_000:
                    raise MediaError(app + " response exceeds the supported library size.")
            if app == 'plex':
                return ET.fromstring(raw) if raw else None
            return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise MediaError(f'{app} returned HTTP {e.code}; request outcome may be uncertain for writes.') from None
        except Exception:
            raise MediaError(f'{app} connection or response unavailable; no credential/error details returned.') from None

    def nzb(self, method, params=None):
        value = self.api('nzbget', '/jsonrpc', 'POST', {'method': method, 'params': params or [], 'id': 1})
        if value.get('error'):
            raise MediaError('NZBGet operation failed.')
        return value['result']

    def arr(self, kind, endpoint, method='GET', data=None):
        return self.api('radarr' if kind == 'movie' else 'sonarr', '/api/v3/'+endpoint, method, data)

    def plex(self, query, kind='movie', year=None):
        # Restrict to the actual Movies/TV libraries, excluding personal videos.
        result = []
        for section in self.config['plex']['section_ids'][kind]:
            root = self.api('plex', '/library/sections/'+section+'/all?'+urllib.parse.urlencode({'title': query}))
            for x in root:
                if x.get('type') != ('movie' if kind == 'movie' else 'show'):
                    continue
                title = x.get('title', '')
                if norm(query) not in norm(title):
                    continue
                if year and int(x.get('year', '0')) != year:
                    continue
                key = x.get('ratingKey')
                detail = self.api('plex', '/library/metadata/'+key)
                metadata = list(detail)[0]
                ids = [z.get('id', '') for z in metadata.findall('Guid')]
                result.append({'rating_key': key, 'title': title, 'year': int(x.get('year', '0')), 'kind': kind, 'guids': ids, 'has_media': bool(metadata.findall('.//Part')), 'duration_minutes': round(int(x.get('duration', '0'))/60000), 'genres': [z.get('tag') for z in metadata.findall('Genre')], 'summary': (x.get('summary') or '')[:800], 'view_count': int(x.get('viewCount', '0')), 'plex_url': self.config['plex']['url'].rstrip('/')+'/web/index.html#!/server/'+self.config['plex'].get('server_id', '')+'/details?key='+urllib.parse.quote('/library/metadata/'+key, safe='')})
        return result[:30]

    def plex_match(self, item, kind):
        ident = str(item['tmdbId' if kind == 'movie' else 'tvdbId'])
        scheme = 'tmdb://' if kind == 'movie' else 'tvdb://'
        candidates = self.plex(item['title'], kind, item.get('year') or None)
        exact = [x for x in candidates if scheme+ident in x['guids']]
        if exact:
            return exact
        # A conflicting external identifier never falls back to a title match.
        return [x for x in candidates if norm(x['title']) == norm(item['title']) and not any(g.startswith(scheme) for g in x['guids'])]

    def resolve(self, query, kind, year=None, external_id=None):
        managed = self.arr(kind, 'movie' if kind == 'movie' else 'series')
        idkey = 'tmdbId' if kind == 'movie' else 'tvdbId'
        matches = [x for x in managed if (x.get(idkey) == external_id if external_id else norm(x['title']) == norm(query)) and (not year or x.get('year') == year)]
        if not matches:
            term = ('tmdb:' if kind == 'movie' else 'tvdb:')+str(external_id) if external_id else query
            values = self.arr(kind, ('movie' if kind == 'movie' else 'series')+'/lookup?'+urllib.parse.urlencode({'term': term}))
            if isinstance(values, dict): values = [values]
            matches = [x for x in values if (x.get(idkey) == external_id if external_id else norm(query) in {norm(x.get('title', '')), norm(x.get('originalTitle', ''))}) and (not year or x.get('year') == year)]
            if len(matches) != 1:
                return None, {'state': 'needs_selection', 'message': 'Specify the title and year or external ID before downloading.', 'candidates': [clean_movie(x, kind) for x in values[:10]]}
        unique = {x[idkey]: x for x in matches}
        if len(unique) != 1:
            return None, {'state': 'needs_selection', 'message': 'More than one title matches; specify the year.', 'candidates': [clean_movie(x, kind) for x in matches[:10]]}
        return next(iter(unique.values())), None

    def check(self, query, kind='movie', year=None):
        matches = self.plex(query, kind, year)
        return {'state': 'in_plex' if matches else 'not_found_in_plex', 'matches': matches, 'message': 'Library lookup only; nothing was downloaded.'}

    def recommend(self, genre=None, max_minutes=None, unwatched=False, limit=5):
        section = self.config['plex']['section_ids']['movie'][0]
        params = {'type': 1, 'sort': 'rating:desc', 'X-Plex-Container-Start': 0, 'X-Plex-Container-Size': 200}
        if genre: params['genre'] = genre
        if unwatched: params['unwatched'] = 1
        if max_minutes: params['duration<='] = max_minutes*60000
        root = self.api('plex', '/library/sections/'+section+'/all?'+urllib.parse.urlencode(params))
        out = []
        for x in root:
            minutes = round(int(x.get('duration', '0'))/60000)
            if max_minutes and minutes > max_minutes: continue
            if unwatched and int(x.get('viewCount', '0')): continue
            if not x.findall('.//Part'): continue
            out.append({'title': x.get('title'), 'year': x.get('year'), 'duration_minutes': minutes, 'genres': [g.get('tag') for g in x.findall('Genre')], 'summary': (x.get('summary') or '')[:700], 'view_count': int(x.get('viewCount', '0'))})
            if len(out) >= min(limit, 20): break
        return {'recommendations': out, 'watch_history_scope': 'Plex token account; may differ from the viewer speaking to Alexa.'}

    def update(self, job_id, phase, detail, **fields):
        fields.update(phase=phase, detail=detail, updated=time.time())
        with self.db() as db:
            db.execute('UPDATE jobs SET '+','.join(k+'=?' for k in fields)+' WHERE id=?', [*fields.values(), job_id])

    def request(self, query, kind='movie', year=None, external_id=None, seasons=None):
        with self.mutation_lock():
            item, choice = self.resolve(query, kind, year, external_id)
            if choice: return choice
            if kind == 'series' and not seasons:
                return {'state': 'needs_season', 'message': 'Which season should I download? Specify a season number, latest, or all.', 'title': item['title'], 'seasons': clean_movie(item, kind)['seasons']}
            available = [s['seasonNumber'] for s in item.get('seasons', []) if s.get('seasonNumber', 0) > 0]
            if kind == 'series':
                selected = available if seasons == 'all' else [max(available)] if seasons == 'latest' and available else [int(x) for x in seasons.split(',')] if seasons not in ('all', 'latest') else []
                if not selected or not set(selected) <= set(available):
                    raise MediaError('Requested season is not present in the series metadata.')
            else: selected = []
            plex = self.plex_match(item, kind)
            if kind == 'movie' and any(x['has_media'] for x in plex):
                return {'state': 'ready', 'title': item['title'], 'message': 'Already in Plex; no duplicate download started.', 'plex': plex}
            identity = kind+':'+str(item['tmdbId' if kind == 'movie' else 'tvdbId'])+':'+','.join(map(str, sorted(selected)))
            with self.db() as db:
                prior = db.execute('SELECT * FROM jobs WHERE identity=?', (identity,)).fetchone()
                if prior: return {'state': prior['phase'], 'job': dict(prior), 'message': 'Existing tracked request returned; no duplicate search submitted.'}
                job = str(uuid.uuid4())[:12]
                now = time.time()
                db.execute('INSERT INTO jobs (id,identity,kind,external_id,title,year,seasons,arr_id,phase,detail,created,updated,last_progress,progress) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (job, identity, kind, item['tmdbId' if kind == 'movie' else 'tvdbId'], item['title'], item.get('year'), json.dumps(selected), item.get('id'), 'submitting', 'Request recorded before submission.', now, now, now, ''))
            try:
                app = 'radarr' if kind == 'movie' else 'sonarr'
                cfg = self.config[app]
                if not item.get('id'):
                    payload = {k: item[k] for k in ['title', 'year', 'tmdbId', 'tvdbId', 'titleSlug', 'images', 'seasons'] if k in item}
                    payload.update(qualityProfileId=cfg['quality_profile_id'], rootFolderPath=cfg['root_folder'], monitored=True)
                    if kind == 'movie':
                        payload.update(minimumAvailability='released', addOptions={'searchForMovie': True})
                    else:
                        payload['seasons'] = [{'seasonNumber': n['seasonNumber'], 'monitored': n['seasonNumber'] in selected} for n in item['seasons']]
                        payload.update(seasonFolder=True, seriesType='standard', addOptions={'searchForMissingEpisodes': False})
                    item = self.arr(kind, 'movie' if kind == 'movie' else 'series', 'POST', payload)
                    self.update(job, 'searching', 'Added to '+app+'.', arr_id=item['id'])
                    if kind == 'series':
                        self.arr(kind, 'command', 'POST', {'name': 'SeasonSearch', 'seriesId': item['id'], 'seasonNumber': selected[0]})
                        for season in selected[1:]: self.arr(kind, 'command', 'POST', {'name': 'SeasonSearch', 'seriesId': item['id'], 'seasonNumber': season})
                else:
                    if not item.get('monitored') or kind == 'series':
                        item['monitored'] = True
                        if kind == 'series':
                            for season in item['seasons']:
                                if season['seasonNumber'] in selected: season['monitored'] = True
                        self.arr(kind, ('movie' if kind == 'movie' else 'series')+'/'+str(item['id']), 'PUT', item)
                    if kind == 'movie' and item.get('hasFile'):
                        self.update(job, 'awaiting_plex', 'File already imported; checking Plex without downloading again.')
                    elif kind == 'movie':
                        self.arr(kind, 'command', 'POST', {'name': 'MoviesSearch', 'movieIds': [item['id']]})
                    else:
                        episodes = self.arr(kind, 'episode?seriesId='+str(item['id']))
                        missing = [e['id'] for e in episodes if e.get('seasonNumber') in selected and not e.get('hasFile') and e.get('airDateUtc') and e['airDateUtc'] <= datetime.now(timezone.utc).isoformat().replace('+00:00','Z')]
                        if missing:
                            self.arr(kind, 'command', 'POST', {'name': 'EpisodeSearch', 'episodeIds': missing})
                self.update(job, 'searching' if not item.get('hasFile') else 'awaiting_plex', 'Request accepted. Availability in Plex still needs verification.', arr_id=item['id'])
            except Exception:
                self.update(job, 'uncertain', 'Submission outcome uncertain; monitoring will reconcile by external ID without replaying the download request.')
            return self.status(job)

    def status(self, query=None):
        with self.db() as db:
            rows = db.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT 100').fetchall()
        if query:
            rows = [x for x in rows if x['id'] == query or norm(query) in norm(x['title'])]
        return {'jobs': [dict(x) for x in rows[:20]], 'download_client': {k: v for k, v in self.nzb('status').items() if k in ['DownloadPaused', 'PostPaused', 'DownloadRate', 'RemainingSizeMB', 'FreeDiskSpaceMB']}, 'message': 'Ready means a verified Plex entry with media, not merely a completed download.'}

    def progress_job(self, row):
        job = dict(row);kind = job['kind'];app = 'radarr' if kind == 'movie' else 'sonarr'
        items = self.arr(kind, 'movie' if kind == 'movie' else 'series')
        field = 'tmdbId' if kind == 'movie' else 'tvdbId'
        item = next((x for x in items if x.get(field) == job['external_id']), None)
        if not item:
            self.update(job['id'], 'needs_attention', 'No managed title found after submission. No automatic resubmission was made.')
            return
        if not job['arr_id']:
            self.update(job['id'], 'searching', 'Reconciled managed title after uncertain submission.', arr_id=item['id'])
        plex = self.plex_match(item, kind)
        episodes = []; selected = json.loads(job['seasons'])
        if kind == 'movie':
            imported = bool(item.get('hasFile'));ready = any(p['has_media'] for p in plex)
        else:
            all_episodes = self.arr(kind, 'episode?seriesId='+str(item['id']))
            aired = [e for e in all_episodes if e.get('seasonNumber') in selected and e.get('airDateUtc') and e['airDateUtc'] <= datetime.now(timezone.utc).isoformat().replace('+00:00','Z')]
            episodes = aired
            imported = bool(aired) and all(e.get('hasFile') for e in aired)
            plex_episodes = set()
            for p in plex:
                children = self.api('plex', '/library/metadata/'+p['rating_key']+'/children')
                for season in children:
                    if int(season.get('index','-1')) not in selected: continue
                    eroot = self.api('plex', '/library/metadata/'+season.get('ratingKey')+'/children')
                    for e in eroot:
                        if e.findall('.//Part'): plex_episodes.add((int(season.get('index')), int(e.get('index','-1'))))
            ready = imported and all((e['seasonNumber'], e['episodeNumber']) in plex_episodes for e in aired)
        if ready:
            self.update(job['id'], 'ready', 'Verified in Plex with playable media'+(' for all currently aired requested episodes.' if kind == 'series' else '.'))
            return
        queue = self.arr(kind, 'queue?includeUnknownSeriesItems=false&includeUnknownMovieItems=false&pageSize=1000').get('records', [])
        own = [q for q in queue if q.get('movieId' if kind == 'movie' else 'seriesId') == item['id']]
        if kind == 'series': own = [q for q in own if not q.get('episode') or q['episode'].get('seasonNumber') in selected]
        nzb = self.nzb('status')
        if imported:
            if job['scans'] < 3 and time.time()-job['last_scan'] >= 600:
                cfg = self.config['path_maps'][app]
                host_path = next((target+item['path'][len(source):] for source,target in cfg.items() if item.get('path') == source or item.get('path','').startswith(source+'/')), None)
                if host_path:
                    for section in self.config['plex']['section_ids'][kind]:
                        self.api('plex', '/library/sections/'+section+'/refresh?'+urllib.parse.urlencode({'path':host_path}))
                    self.update(job['id'], 'awaiting_plex', 'Imported file found; targeted Plex refresh requested. Visibility not yet verified.', scans=job['scans']+1, last_scan=time.time())
            else:
                self.update(job['id'], 'needs_attention' if job['scans'] >= 3 else 'awaiting_plex', 'Imported media is not yet verified in Plex; bounded scan attempts are monitored.')
        elif own:
            errors = [q for q in own if q.get('trackedDownloadState') in ('importFailed','downloadFailed') or q.get('trackedDownloadStatus') in ('error','warning')]
            progress = json.dumps([{k:q.get(k) for k in ['sizeleft','status','trackedDownloadState','downloadId']} for q in own],sort_keys=True)
            fields = {'progress':progress}
            if progress != job['progress']: fields['last_progress'] = time.time()
            stalled = time.time()-job['last_progress'] > 7200 and progress == job['progress']
            phase = 'needs_attention' if errors or stalled else 'downloading'
            detail = 'NZBGet globally paused; existing pause preserved.' if nzb.get('DownloadPaused') else 'Download/import needs attention; no files or queue entries deleted.' if errors or stalled else 'Tracking the managed download through import.'
            # Cross-check only matching downloader IDs, rather than displaying unrelated queue items.
            ids = {str(q.get('downloadId')) for q in own}
            groups = self.nzb('listgroups')
            matching = [g for g in groups if str(g.get('NZBID')) in ids]
            if not matching:
                matching = [g for g in self.nzb('history',[False]) if str(g.get('NZBID')) in ids]
            if any(str(g.get('Status','')).startswith('FAILURE') for g in matching):
                phase = 'needs_attention'; detail = 'The matching NZBGet history entry reports failure. No destructive retry was performed.'
            if matching: detail += ' Matching NZBGet job verified.'
            self.update(job['id'], phase, detail, **fields)
        else:
            age = time.time()-job['created']
            detail = 'NZBGet is globally paused.' if nzb.get('DownloadPaused') else 'Waiting for a qualifying release/search result. No repeated automatic searches submitted.'
            if kind == 'series' and not episodes: detail = 'Waiting for requested episodes to air or metadata to arrive.'
            self.update(job['id'], 'needs_attention' if age > 86400*7 else 'searching', detail)

    def monitor(self):
        with self.mutation_lock():
            with self.db() as db:
                rows = db.execute("SELECT * FROM jobs WHERE phase != 'ready' AND created > ? ORDER BY created LIMIT 100",(time.time()-86400*30,)).fetchall()
            outcomes = []
            for row in rows:
                try:
                    self.progress_job(row); outcomes.append({'id':row['id'],'checked':True})
                except Exception:
                    self.update(row['id'], row['phase'], 'Monitoring source unavailable; retry on next scheduled check. Prior phase retained.')
                    outcomes.append({'id':row['id'],'checked':False})
        return {'checked':outcomes}

    def dispatch(self, args):
        action = args.get('action')
        allowed = {'check':{'query','kind','year'},'request':{'query','kind','year','external_id','seasons'},'status':{'query'},'recommend':{'genre','max_minutes','unwatched','limit'}}
        if action not in allowed or set(args)-allowed[action]-{'action'}: raise MediaError('Unsupported media operation or arguments.')
        kwargs = {k:v for k,v in args.items() if k!='action'}
        if action in ('request','check'):
            if not isinstance(kwargs.get('query'),str) or not 1 <= len(kwargs['query']) <= 200: raise MediaError('A title between 1 and 200 characters is required.')
            if kwargs.get('kind','movie') not in ('movie','series'): raise MediaError('Unsupported media kind.')
        return getattr(self,action)(**kwargs)

    def sanitize(self, value):
        text = json.dumps(value,ensure_ascii=False)
        for app in ['radarr','sonarr','plex','nzbget']:
            for key in ['api_key','token','password']:
                secret = self.config[app].get(key)
                if secret: text = text.replace(secret,'[REDACTED]')
        return text


def cached_status(query=None, home=HOME):
    path=Path(home)/'jobs.sqlite3'
    if not path.exists(): return {'jobs':[]}
    db=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    try: rows=db.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT 100').fetchall()
    finally:db.close()
    return {'jobs':[dict(x) for x in rows if not query or x['id']==query or norm(query) in norm(x['title'])][:20], 'message':'Last background-monitor snapshot; no network call on the Alexa response path.'}


def voice_answer(result):
    if result.get('error'):
        speech = result['error']
    elif result.get('state')=='ready':
        speech = result.get('message') or 'The title is ready in Plex.'
    elif result.get('state')=='needs_season':
        speech = 'Which season of '+result['title']+' should I download? Say the season number, latest season, or all seasons.'
    elif result.get('state')=='needs_selection':
        names = [str(x.get('title'))+' '+str(x.get('year') or '') for x in result.get('candidates',[])[:3]]
        speech = 'Please specify the title and year. Possible matches: '+', '.join(names)
    elif 'matches' in result:
        speech = 'I found '+', '.join(x['title']+' '+str(x.get('year') or '') for x in result['matches'][:3])+' in Plex.' if result['matches'] else 'I did not find that title in Plex. This was a library check only.'
    elif result.get('jobs'):
        speech = '; '.join(x['title']+': '+x['phase'].replace('_',' ')+'. '+x['detail'] for x in result['jobs'][:2])
        if result.get('download_client',{}).get('DownloadPaused'): speech += ' NZBGet is paused.'
    elif result.get('recommendations'):
        speech = 'You could watch '+', '.join(x['title']+' from '+str(x['year']) for x in result['recommendations'][:3])+'.'
    else:
        speech = result.get('message') or 'No matching tracked media requests were found.'
    sections = [speech]
    for item in result.get('matches', []) + result.get('recommendations', []):
        heading = item['title'] + (' (' + str(item['year']) + ')' if item.get('year') else '')
        details = [heading]
        if item.get('duration_minutes'): details.append(str(item['duration_minutes']) + ' minutes')
        if item.get('genres'): details.append(', '.join(item['genres']))
        if item.get('summary'): details.append(item['summary'])
        sections.append('\n'.join(details))
    for job in result.get('jobs', []):
        sections.append(job['title'] + ': ' + job['phase'].replace('_', ' ') + '\n' + job['detail'])
    if result.get('message') and result['message'] != speech: sections.append(result['message'])
    return {'title':'Media Concierge','speech':' '.join(speech.split()[:78]),'display':'\n\n'.join(sections)}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('action',choices=['check','request','status','recommend','monitor','stdio'])
    parser.add_argument('query',nargs='?')
    parser.add_argument('--kind',choices=['movie','series'])
    parser.add_argument('--year',type=int)
    parser.add_argument('--external-id',type=int)
    parser.add_argument('--seasons')
    parser.add_argument('--genre')
    parser.add_argument('--max-minutes',type=int)
    parser.add_argument('--unwatched',action='store_true',default=None)
    parser.add_argument('--limit',type=int)
    args = {k:v for k,v in vars(parser.parse_args()).items() if v is not None}
    concierge = Concierge()
    def emit(request):
        try: print(concierge.sanitize(concierge.dispatch(request)),flush=True)
        except MediaError as e: print(json.dumps({'error':str(e)}),flush=True)
        except Exception: print(json.dumps({'error':'Media operation failed; outcome may be uncertain for a write. Check tracked requests before retrying.'}),flush=True)
    if args['action']=='stdio':
        for line in sys.stdin:
            if len(line)>4096: continue
            try: emit(json.loads(line))
            except Exception: print('{"error":"Invalid request"}',flush=True)
    elif args['action']=='monitor': print(concierge.sanitize(concierge.monitor()))
    else: emit(args)


if __name__=='__main__': main()
