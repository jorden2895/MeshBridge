import unittest

from meshtastic.util import generate_channel_hash

from meshtastic_codec import channel_hash, crypt_payload, normalize_channel_key


class MeshtasticCodecTests(unittest.TestCase):
    def test_hash_matches_official_meshtastic_implementation(self):
        for encoded_key in ("AQ==", "Ag==", "1PG7OiApB1nwvP+rz05pAQ=="):
            with self.subTest(encoded_key=encoded_key):
                key = normalize_channel_key(encoded_key)
                self.assertEqual(
                    channel_hash("LongFast", key),
                    generate_channel_hash("LongFast", encoded_key),
                )

    def test_crypt_payload_round_trip(self):
        key = normalize_channel_key("AQ==")
        plaintext = "測試 message".encode()
        encrypted = crypt_payload(plaintext, key, packet_id=123, sender_id=456)
        self.assertNotEqual(encrypted, plaintext)
        self.assertEqual(
            crypt_payload(encrypted, key, packet_id=123, sender_id=456),
            plaintext,
        )


if __name__ == "__main__":
    unittest.main()
