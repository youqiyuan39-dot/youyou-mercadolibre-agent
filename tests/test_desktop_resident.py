"""No real GUI, network or service process is started by these tests."""
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'desktop' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lifetime = load('resident_lifetime_test', 'backend_lifecycle.py')
app = load('resident_app_test', 'app.py')


class ResidentTests(unittest.TestCase):
    def test_local_health_explicitly_bypasses_proxy(self):
        connection=Mock()
        response=connection.getresponse.return_value
        response.status=200
        response.read.return_value=b'{"ok":true}'
        with patch.object(app.http.client,'HTTPConnection',return_value=connection) as direct, \
             patch.object(app.urllib.request,'build_opener',side_effect=FileNotFoundError()) as proxy:
            self.assertEqual(app.health(),{'ok':True})
        proxy.assert_not_called()
        direct.assert_called_once_with('127.0.0.1',8790,timeout=3)
        connection.request.assert_called_once_with('GET','/api/health')
        connection.close.assert_called_once()

    def test_resident_close_is_idempotent_and_has_no_service_effect(self):
        health = Mock(side_effect=AssertionError('Resident close must not access service'))
        process = Mock()
        with patch.object(lifetime, 'build_opener') as network:
            client = lifetime.BackendLifetime(ROOT, app.URL, health, process, mode='resident')
            client.close()
            client.close()
        self.assertTrue(client.closed)
        health.assert_not_called()
        network.assert_not_called()
        self.assertEqual(process.mock_calls, [])

    def test_default_retains_window_bound_behavior(self):
        process = Mock()
        process.poll.return_value = None
        with patch.object(lifetime.time, 'sleep'):
            client = lifetime.BackendLifetime(ROOT, app.URL, lambda: None, process)
            client.close()
        self.assertEqual(client.mode, 'window_bound')
        process.terminate.assert_called_once()

    def test_resident_missing_backend_never_spawns(self):
        with patch.dict(sys.modules, {'backend_lifecycle': lifetime}), \
             patch.object(app, 'health', return_value=None), \
             patch.object(app.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(RuntimeError, '未启动或修改服务'):
                app.ensure_server(ROOT, mode='resident')
            spawn.assert_not_called()

    def test_resident_attaches_matching_service(self):
        state = {'workspace': str(ROOT), 'version': '0.3.0-evidence',
                 'desktop_lifecycle': 'WINDOW_BOUND_V1'}
        with patch.dict(sys.modules, {'backend_lifecycle': lifetime}), \
             patch.object(app, 'health', return_value=state), \
             patch.object(app.subprocess, 'Popen') as spawn:
            client = app.ensure_server(ROOT, mode='resident')
            self.assertEqual(client.mode, 'resident')
            self.assertIsNone(client.process)
            spawn.assert_not_called()

    def test_resident_rejects_foreign_service(self):
        state = {'workspace': str(ROOT.parent), 'version': '0.3.0-evidence'}
        with patch.dict(sys.modules, {'backend_lifecycle': lifetime}), \
             patch.object(app, 'health', return_value=state), \
             patch.object(app.subprocess, 'Popen') as spawn:
            with self.assertRaises(RuntimeError):
                app.ensure_server(ROOT, mode='resident')
            spawn.assert_not_called()

    def test_unknown_mode_rejected_before_service_access(self):
        with patch.dict(sys.modules, {'backend_lifecycle': lifetime}), \
             patch.object(app, 'health') as health:
            with self.assertRaises(ValueError):
                app.ensure_server(ROOT, mode='invalid')
            health.assert_not_called()
        with self.assertRaises(ValueError):
            lifetime.BackendLifetime(ROOT, app.URL, Mock(), mode='invalid')
