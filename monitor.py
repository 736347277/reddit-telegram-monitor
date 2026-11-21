import argparse
import csv
import os
import re
import sqlite3
import time
from pathlib import Path

import praw
import requests
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / '.env')
DB = BASE / 'monitor.sqlite3'
INTERVAL = 300
LIMIT = 100
ID_RE = re.compile(r'reddit\.com/r/[^/]+/comments/([a-z0-9]+)', re.I)
SHORT_RE = re.compile(r'^[a-z0-9]{5,10}$', re.I)


def post_id(value):
    value = value.strip()
    match = ID_RE.search(value)
    if match:
        return match.group(1).lower()
    if SHORT_RE.fullmatch(value):
        return value.lower()
    raise ValueError('Use a full reddit.com/r/.../comments/ID/ URL or a post ID (not /s/ links).')


def db():
    connection = sqlite3.connect(DB)
    connection.execute('''CREATE TABLE IF NOT EXISTS posts (
      id TEXT PRIMARY KEY, threshold INTEGER NOT NULL CHECK(threshold >= 0),
      notified INTEGER NOT NULL DEFAULT 0, last_score INTEGER,
      title TEXT, url TEXT)''')
    columns = {row[1] for row in connection.execute('PRAGMA table_info(posts)')}
    if 'baseline_score' not in columns:
        connection.execute('ALTER TABLE posts ADD COLUMN baseline_score INTEGER')
    connection.commit()
    return connection


def add(connection, value, threshold):
    pid = post_id(value)
    if threshold <= 0:
        raise ValueError('Increase must be a positive integer')
    existing = connection.execute('SELECT id FROM posts WHERE id=?', (pid,)).fetchone()
    if not existing and connection.execute('SELECT COUNT(*) FROM posts').fetchone()[0] >= LIMIT:
        raise ValueError('Maximum 100 posts')
    connection.execute('''INSERT INTO posts(id, threshold) VALUES(?,?)
        ON CONFLICT(id) DO UPDATE SET threshold=excluded.threshold''', (pid, threshold))
    connection.commit()
    return pid


def notify(message):
    token = os.environ['TELEGRAM_BOT_TOKEN']
    chat_id = os.environ['TELEGRAM_CHAT_ID']
    response = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                             json={'chat_id': chat_id, 'text': message}, timeout=20)
    response.raise_for_status()
    if not response.json().get('ok'):
        raise RuntimeError('Telegram rejected message')


def run():
    reddit = praw.Reddit(client_id=os.environ['REDDIT_CLIENT_ID'],
                         client_secret=os.environ['REDDIT_CLIENT_SECRET'],
                         user_agent=os.environ['REDDIT_USER_AGENT'])
    connection = db()
    print('Monitoring started. Ctrl+C to stop.', flush=True)
    try:
        while True:
            started = time.monotonic()
            rows = connection.execute('SELECT id,threshold,baseline_score FROM posts ORDER BY id').fetchall()
            for pid, threshold, baseline in rows:
                try:
                    submission = reddit.submission(id=pid)
                    score = int(submission.score)
                    title = submission.title
                    url = f'https://www.reddit.com{submission.permalink}'
                    if baseline is None:
                        baseline = score
                        print(f'{pid}: initial baseline={baseline}', flush=True)
                    elif score - baseline >= threshold:
                        notify(f'📈 Reddit score increased!\n{title}\nPrevious alert baseline: {baseline}\nCurrent score: {score}\nIncrease: +{score - baseline}\n{url}')
                        baseline = score  # Advance only after Telegram confirms delivery.
                        print(f'{pid}: alert sent, new baseline={baseline}', flush=True)
                    connection.execute('UPDATE posts SET last_score=?,title=?,url=?,baseline_score=? WHERE id=?',
                                       (score, title, url, baseline, pid))
                    connection.commit()
                    print(f'{pid}: score={score}, baseline={baseline}, next={baseline + threshold}', flush=True)
                except Exception as error:
                    print(f'ERROR {pid}: {type(error).__name__}: {error}', flush=True)
            time.sleep(max(0, INTERVAL - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print('\nMonitoring stopped.', flush=True)
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description='Monitor increases in Reddit post scores (up to 100 posts)')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('add'); p.add_argument('url'); p.add_argument('threshold', type=int)
    p = sub.add_parser('import'); p.add_argument('csv_file')
    p = sub.add_parser('remove'); p.add_argument('urls', nargs='+')
    sub.add_parser('list'); sub.add_parser('run'); sub.add_parser('test-telegram')
    args = parser.parse_args()
    connection = db()
    if args.command == 'add':
        print('Added:', add(connection, args.url, args.threshold))
    elif args.command == 'import':
        with open(args.csv_file, encoding='utf-8-sig', newline='') as handle:
            entries = list(csv.DictReader(handle))
        parsed = [(post_id(row['url']), int(row['threshold'])) for row in entries]
        if any(threshold <= 0 for _, threshold in parsed):
            raise ValueError('Increase thresholds must be positive')
        unique = {pid for pid, _ in parsed}
        existing = {row[0] for row in connection.execute('SELECT id FROM posts')}
        if len(existing | unique) > LIMIT:
            raise ValueError('Import exceeds 100 total unique posts')
        for pid, threshold in parsed:
            add(connection, pid, threshold)
        print(f'Imported {len(parsed)} rows ({len(unique)} unique IDs).')
    elif args.command == 'remove':
        ids = [post_id(value) for value in args.urls]
        connection.executemany('DELETE FROM posts WHERE id=?', [(pid,) for pid in ids])
        connection.commit()
        print(f'Removed up to {len(ids)} posts')
    elif args.command == 'list':
        for pid, threshold, score, baseline in connection.execute('SELECT id,threshold,last_score,baseline_score FROM posts ORDER BY id'):
            next_target = baseline + threshold if baseline is not None else 'waiting for first reading'
            print(f'{pid} | increase={threshold} | score={score} | baseline={baseline} | next={next_target}')
    elif args.command == 'test-telegram':
        notify('Reddit Monitor: Telegram test successful!')
        print('Sent')
    elif args.command == 'run':
        run()
    connection.close()


if __name__ == '__main__':
    main()
