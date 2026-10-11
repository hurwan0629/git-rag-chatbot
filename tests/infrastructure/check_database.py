"""Check the real Compose DB in an isolated project, then remove only its test data."""

import json
import os
from pathlib import Path
import secrets
import subprocess
import uuid


ROOT = Path(__file__).resolve().parents[2]


def main():
    project = 'git-rag-db-check-' + uuid.uuid4().hex[:12]
    environment = os.environ.copy()
    environment.update(COMPOSE_PROJECT_NAME=project, POSTGRES_DB='git_rag_check',
                       POSTGRES_USER='git_rag_check', POSTGRES_PORT='0')
    environment.pop('POSTGRES_PASSWORD', None)
    compose = ['docker', 'compose', '--env-file', str(ROOT / '.env.example'),
               '-f', str(ROOT / 'compose.yaml'), '--project-name', project]

    def run(arguments, *, check=True, timeout=120, input=None):
        result = subprocess.run(arguments, cwd=ROOT, env=environment,
                                capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=timeout, input=input)
        if check and result.returncode:
            # Compose config and container logs may contain credentials; keep them private.
            raise RuntimeError('Command failed: ' + ' '.join(arguments[:3])
                               + '; exit=' + str(result.returncode))
        return result

    def require(condition, message):
        if not condition:
            raise RuntimeError(message)

    empty = run(compose + ['config', '--quiet'], check=False)
    require(empty.returncode != 0 and 'POSTGRES_PASSWORD' in empty.stderr,
            'Empty password must be rejected before startup.')
    print('PASS: empty password rejected')

    environment['POSTGRES_PASSWORD'] = secrets.token_hex(24) + '$#'
    config = json.loads(run(compose + ['config', '--format', 'json']).stdout)
    require(config['name'] == project, 'Unexpected project name.')
    require(set(config['services']) == {'db'}, 'Unexpected Compose services.')
    require(set(config['services']['db']['environment']) ==
            {'POSTGRES_DB', 'POSTGRES_USER', 'POSTGRES_PASSWORD'},
            'Database container must receive only database settings.')
    require(config['services']['db']['ports'][0]['host_ip'] == '127.0.0.1',
            'Database port must bind to localhost.')
    password = environment['POSTGRES_PASSWORD']
    # Compose may escape dollar signs when serializing its reusable configuration.
    require(config['services']['db']['environment']['POSTGRES_PASSWORD'] in
            (password, password.replace('$', '$$')),
            'Unexpected serialized password value.')
    volume_names = [value['name'] for value in config['volumes'].values()]
    require(volume_names == [project + '_postgres_data'], 'Unexpected volume ownership.')
    require(all(value['name'].startswith(project + '_')
                for value in config['networks'].values()), 'Unexpected network ownership.')
    bind = next(value for value in config['services']['db']['volumes']
                if value['type'] == 'bind')
    require(Path(bind['source']).resolve() == (ROOT / 'docker/postgres/init').resolve()
            and bind['read_only'], 'Initialization SQL mount must be read-only.')
    print('PASS: Compose configuration, secret isolation, localhost binding')

    def sql(statement):
        return run(compose + ['exec', '-T', 'db', 'psql', '-v', 'ON_ERROR_STOP=1',
                              '-U', 'git_rag_check', '-d', 'git_rag_check', '-At'],
                   input=statement).stdout.strip()

    try:
        run(compose + ['up', '-d', '--wait', '--wait-timeout', '60', 'db'])
        state = json.loads(run(compose + ['ps', '--format', 'json', 'db']).stdout)
        if isinstance(state, list):
            state = state[0]
        require(state['Health'] == 'healthy', 'Database health check did not pass.')
        version = sql("SELECT extversion FROM pg_extension WHERE extname='vector';")
        require(version == '0.8.7', 'Vector extension missing or wrong version.')
        require(sql('SHOW server_version;').startswith('17.'), 'Wrong PostgreSQL major version.')
        print('PASS: PostgreSQL 17 healthy; pgvector 0.8.7 initialized')

        client = ['db', 'psql', '-h', 'db', '-v', 'ON_ERROR_STOP=1',
                  '-U', 'git_rag_check', '-d', 'git_rag_check', '-At']
        authenticated = run(compose + ['exec', '-T', '-e', 'PGPASSWORD=' + password] + client,
                            input='SELECT 1;')
        require(authenticated.stdout.strip() == '1', 'TCP password authentication failed.')
        rejected = run(compose + ['exec', '-T', '-e', 'PGPASSWORD=invalid-check-password'] + client,
                       input='SELECT 1;', check=False)
        require(rejected.returncode != 0, 'Wrong password was accepted over TCP.')
        print('PASS: TCP authentication and literal password characters')

        result = sql('''BEGIN;
CREATE TEMP TABLE vector_check (id integer PRIMARY KEY, embedding vector(384));
INSERT INTO vector_check VALUES
  (1, (ARRAY[1::real] || array_fill(0::real, ARRAY[383]))::vector(384)),
  (2, (ARRAY[0::real,1::real] || array_fill(0::real, ARRAY[382]))::vector(384));
SELECT id FROM vector_check
ORDER BY embedding <=> (ARRAY[1::real] || array_fill(0::real, ARRAY[383]))::vector(384), id;
ROLLBACK;
''')
        require([line for line in result.splitlines() if line.isdigit()] == ['1', '2'],
                '384-dimensional cosine ranking is incorrect.')
        print('PASS: vector(384) insert and cosine search')

        sql('CREATE TABLE persistence_check (id integer PRIMARY KEY); '
            'INSERT INTO persistence_check VALUES (1);')
        run(compose + ['down'])
        run(compose + ['up', '-d', '--wait', '--wait-timeout', '60', 'db'])
        require(sql('SELECT count(*) FROM persistence_check;') == '1',
                'Data did not survive container recreation.')
        print('PASS: data survives down and up')
    finally:
        # Cleanup is restricted to the freshly generated project and verified volume above.
        run(compose + ['down', '--volumes'])
        for name in volume_names:
            require(run(['docker', 'volume', 'inspect', name], check=False).returncode != 0,
                    'Test volume was not removed.')
        print('PASS: isolated test project and volume cleaned up')


if __name__ == '__main__':
    main()
