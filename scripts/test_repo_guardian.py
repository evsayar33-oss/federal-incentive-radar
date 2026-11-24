import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone, timedelta


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


guardian = module('repo_guardian')
probe = module('resource_probe')


class GuardianTests(unittest.TestCase):
    def test_source_blob_checks_git_identity_and_size(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'blob'; p.write_bytes(b'abc')
            sha=hashlib.sha1(b'blob 3\0abc').hexdigest()
            guardian.verify_git_blob(p,sha,3)
            with self.assertRaises(ValueError):guardian.verify_git_blob(p,sha,4)
            p.write_bytes(b'bad')
            with self.assertRaises(ValueError):guardian.verify_git_blob(p,sha,3)

    def test_source_backup_does_not_hide_growth_or_size_warning(self):
        now=datetime.now(timezone.utc)
        item={'branch':'data','path':'prices','bytes':30*guardian.MIB,'backup_verified':True}
        self.assertEqual(guardian.large_file_alerts([item],None,now),[])
        old={'observed_at':(now-timedelta(days=1)).isoformat(),
             'large_files':[dict(item,bytes=28*guardian.MIB)]}
        self.assertTrue(guardian.large_file_alerts([item],old,now))
        self.assertTrue(guardian.large_file_alerts([dict(item,bytes=50*guardian.MIB)],None,now))
        self.assertTrue(guardian.large_file_alerts([dict(item,backup_verified=False)],None,now))

    def test_pagination_reads_beyond_first_page(self):
        with patch.object(guardian, 'api', side_effect=[{'items': list(range(100))}, {'items': [100]}]) as api:
            self.assertEqual(len(guardian.paged('path', 'items')), 101)
            self.assertIn('page=2', api.call_args.args[0])

    def test_only_inactivity_disabled_can_be_enabled(self):
        states = ['active', 'disabled_manually', 'disabled_inactivity', 'disabled_fork']
        workflows = [{'state': s, 'path': s, 'id': i} for i, s in enumerate(states)]
        with patch.object(guardian, 'api') as api:
            guardian.repair_inactivity('a/b', workflows, False)
            api.assert_not_called()
            guardian.repair_inactivity('a/b', workflows, True)
            api.assert_called_once_with('repos/a/b/actions/workflows/2/enable', 'PUT')

    def test_artifacts_only_near_expiry(self):
        now = datetime.now(timezone.utc)
        artifacts = [{'id': i, 'expired': expired, 'expires_at': (now + timedelta(days=d)).isoformat()}
                     for i, d, expired in [(1, 20, False), (2, 6, False), (3, -1, True), (4, 1, False)]]
        self.assertEqual([a['id'] for a in guardian.archive_candidates(artifacts, now)], [4, 2])

    def test_corrupted_archive_cannot_be_verified(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d)/'a', Path(d)/'b'
            a.write_bytes(b'original evidence'); b.write_bytes(b'truncated')
            with self.assertRaises(ValueError): guardian.verify_copy(a, b)
            b.write_bytes(a.read_bytes())
            with self.assertRaises(ValueError): guardian.verify_copy(a, b, 'sha256:wrong')
            self.assertEqual(guardian.verify_copy(a, b), guardian.sha256(a))

    def test_growth_warning_does_not_treat_decrease_as_growth(self):
        now = datetime.now(timezone.utc)
        previous = {'observed_at': (now-timedelta(days=1)).isoformat(), 'metrics': {'bytes': 10}}
        self.assertTrue(guardian.threshold_alerts({'bytes': 95}, {'bytes':100}, previous, now))
        self.assertFalse(guardian.threshold_alerts({'bytes':5}, {'bytes':100}, previous, now))

    def test_resource_samples_are_constant_size_and_keep_minima(self):
        value = {'memory_total':100, 'memory_available':60, 'disk_total':1000, 'disk_free':900}
        previous = probe.accumulate({}, value)
        value.update(memory_available=20, disk_free=500)
        previous = probe.accumulate(previous, value)
        value.update(memory_available=90, disk_free=950)
        result = probe.accumulate(previous, value)
        self.assertEqual(result['min_memory_available'], 20)
        self.assertEqual(result['min_disk_free'], 500)
        self.assertEqual(result['samples'], 3)
        self.assertEqual(len(result), 5)

    def test_api_error_is_not_empty_capacity(self):
        with patch.object(guardian, 'api', side_effect=subprocess.CalledProcessError(1, 'gh')):
            with self.assertRaises(subprocess.CalledProcessError): guardian.paged('path', 'items')

    def test_archive_failure_never_writes_receipt_or_deletes_original(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            calls = []
            def command(*args, **kwargs):
                calls.append(args)
                if args[:2] == ('release', 'download'):
                    dest = Path(args[args.index('--dir') + 1])
                    (dest/'artifact-1.zip').write_bytes(b'wrong')
                return ''
            def download(args, **kwargs):
                kwargs['stdout'].write(b'evidence')
            with patch.object(guardian, 'api', return_value={'id': 7}), \
                 patch.object(guardian, 'paged', return_value=[]), \
                 patch.object(guardian, 'gh', side_effect=command), \
                 patch.object(guardian.subprocess, 'run', side_effect=download):
                with self.assertRaises(ValueError):
                    guardian.preserve_artifact('a/b', {'id':1,'size_in_bytes':8}, 'head')
            uploads = [c for c in calls if c[:2] == ('release','upload')]
            self.assertEqual(len(uploads), 1)
            self.assertTrue(uploads[0][3].endswith('.zip'))
            self.assertFalse(any('DELETE' in c for c in calls))

    def test_full_audit_does_not_starve_pending_archives(self):
        now = datetime.now(timezone.utc)
        artifacts = [{'id':i,'expired':False,'expires_at':(now+timedelta(days=1)).isoformat(),
                      'size_in_bytes':20} for i in [1,2]]
        def request(path, *args):
            if path == 'repos/a/b': return {'default_branch':'main','size':1,'private':False}
            if '/commits/' in path:return {'sha':'head','commit':{'committer':{'date':now.isoformat()}}}
            if '/cache/usage' in path:return {'active_caches_size_in_bytes':0}
            raise AssertionError(path)
        def pages(path, key=None):
            if key=='artifacts':return artifacts
            return []
        old={'archives':[{'id':1,'tag':'old'}],'metrics':{},'observed_at':now.isoformat()}
        with patch.object(guardian,'api',side_effect=request), patch.object(guardian,'paged',side_effect=pages), \
             patch.object(guardian,'read_status',return_value=old), patch.object(guardian,'save_status'), \
             patch.object(guardian,'preserve_artifact',return_value={'release_tag':'new'}) as preserve:
            report=guardian.audit('a/b',True)
            self.assertEqual(report['status'],'ok')
            self.assertEqual(report['archive_pending'],0)
            self.assertEqual(preserve.call_args.args[1]['id'],2)
            self.assertEqual([a['id'] for a in report['archives']],[1,2])


if __name__ == '__main__':
    unittest.main()
