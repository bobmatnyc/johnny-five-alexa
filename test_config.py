import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bridge import Backends, ROOT, load_config, media_module


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'config.json'

    def write(self, data, mode=0o600):
        self.path.write_text(json.dumps(data))
        self.path.chmod(mode)

    def test_private_external_config_is_loaded(self):
        self.write({'ALEXA_SKILL_ID': 'example-skill', 'HERMES_API_KEY': 'not-a-real-key'})
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_config(self.path)['ALEXA_SKILL_ID'], 'example-skill')

    def test_group_readable_config_is_rejected(self):
        self.write({'HERMES_API_KEY': 'not-a-real-key'}, 0o640)
        with self.assertRaises(ValueError) as error:
            load_config(self.path)
        self.assertNotIn('not-a-real-key', str(error.exception))

    def test_config_in_checkout_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'outside'):
            load_config(ROOT / 'config.example.json')

    def test_explicit_missing_config_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'not found'):
            load_config(self.path)

    def test_invalid_config_does_not_disclose_values(self):
        self.write({'unexpected': 'not-a-real-key'})
        with self.assertRaises(ValueError) as error:
            load_config(self.path)
        self.assertNotIn('not-a-real-key', str(error.exception))

    def test_environment_override_remains_compatible(self):
        self.write({'ALEXA_SKILL_ID': 'example-skill'})
        with patch.dict(os.environ, {'ALEXA_SKILL_ID': 'override'}, clear=True):
            self.assertEqual(load_config(self.path)['ALEXA_SKILL_ID'], 'override')

    def media_home(self, mode):
        home = Path(self.temp.name) / 'media'
        home.mkdir(mode=0o700)
        config = home / 'config.json'
        config.write_text(json.dumps({'plex': {'token': 'not-a-real-token'}}))
        config.chmod(mode)
        return home

    def test_readable_media_config_is_rejected(self):
        concierge = media_module()
        with self.assertRaises(concierge.MediaError) as error:
            concierge.Concierge(self.media_home(0o644))
        self.assertNotIn('not-a-real-token', str(error.exception))

    def test_private_media_config_is_loaded(self):
        concierge = media_module().Concierge(self.media_home(0o600))
        self.assertEqual(concierge.config['plex']['token'], 'not-a-real-token')

    def test_unconfigured_codex_is_reported_honestly(self):
        status, answer = Backends({}).run('codex', 'hello', 'example-job')
        self.assertEqual(status, 'unavailable')
        self.assertIn('not connected', answer)


if __name__ == '__main__':
    unittest.main()
