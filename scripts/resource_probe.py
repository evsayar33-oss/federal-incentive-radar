"""Constant-size Linux runner RAM/disk telemetry; never modifies research files."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def snapshot(workspace):
    memory = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        memory[key] = int(value.split()[0]) * 1024
    disk = shutil.disk_usage(workspace)
    return {'memory_total': memory['MemTotal'], 'memory_available': memory['MemAvailable'],
            'disk_total': disk.total, 'disk_free': disk.free}


def accumulate(previous, current):
    return {'samples': previous.get('samples', 0) + 1,
            'memory_total': current['memory_total'], 'disk_total': current['disk_total'],
            'min_memory_available': min(previous.get('min_memory_available', current['memory_available']), current['memory_available']),
            'min_disk_free': min(previous.get('min_disk_free', current['disk_free']), current['disk_free'])}


def sample(folder, workspace, duration=21600):
    result = {}
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline and not (folder / 'stop').exists():
        result = accumulate(result, snapshot(workspace))
        temp = folder / 'usage.tmp'
        temp.write_text(json.dumps(result))
        temp.replace(folder / 'usage.json')
        time.sleep(10)


def main():
    mode = sys.argv[1]
    folder = Path(os.environ.get('RUNNER_TEMP', '/tmp')) / (
        'repo-resource-' + os.environ.get('GITHUB_RUN_ID', 'local') + '-' + os.environ.get('GITHUB_JOB', 'job'))
    if mode == 'sample':
        sample(Path(sys.argv[2]), sys.argv[3])
        return
    if mode == 'start':
        folder.mkdir(exist_ok=True)
        (folder / 'stop').unlink(missing_ok=True)
        shutil.copyfile(__file__, folder / 'probe.py')
        # Initial snapshot covers even very short jobs and makes sampler failure visible.
        (folder / 'usage.json').write_text(json.dumps(accumulate({}, snapshot(os.getcwd()))))
        subprocess.Popen([sys.executable, str(folder / 'probe.py'), 'sample', str(folder), os.getcwd()],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return
    if mode != 'finish':
        raise ValueError('Expected start, sample or finish')
    if not folder.exists():
        print('::warning::Resource telemetry did not start; runner capacity unknown')
        return
    (folder / 'stop').touch()
    result = json.loads((folder / 'usage.json').read_text())
    result = accumulate(result, snapshot(os.getcwd()))
    result['scope'] = 'runner-wide samples every 10s; not exact process peak RSS'
    print(json.dumps(result))
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
            stream.write('\n## Runner capacity\n```json\n' + json.dumps(result, indent=2) + '\n```\n')
    for resource, minimum, total in [('RAM', result['min_memory_available'], result['memory_total']),
                                     ('disk', result['min_disk_free'], result['disk_total'])]:
        if minimum < max(256 * 1024**2, total * 0.05):
            print(f'::warning::{resource} available fell below 5% or 256 MiB; inspect workload before scaling')


if __name__ == '__main__':
    main()
