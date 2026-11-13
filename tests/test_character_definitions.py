import tempfile
import unittest
from pathlib import Path

from scripts.build_character_definitions import build_definitions


class CharacterDefinitionsTests(unittest.TestCase):
    def test_official_entries_preserve_all_readings_and_do_not_infer_variant_meanings(self):
        from scripts import build_character_definitions as builder
        definitions = {'倓': {'mandarin': 'tán'}, '铝': {}, '云': {'definition': 'clouds'}}
        records = [
            {'title': '倓', 'pinyin': 'tán', 'definition': '安靜。', 'entry_id': '123', 'sheet': '原表', 'row': 9},
            {'title': '倓', 'pinyin': 'dàn', 'definition': '別音參見。', 'entry_id': '124', 'sheet': '原表', 'row': 10},
            {'title': '鋁', 'pinyin': 'lǚ', 'definition': '金屬元素。', 'entry_id': '125', 'sheet': '原表', 'row': 11},
            {'title': '云', 'pinyin': 'yún', 'definition': '說。', 'entry_id': '126', 'sheet': '原表', 'row': 12},
        ]
        result = builder.add_official_definitions(definitions, records, {'source_url': 'https://example.test/official.zip', 'sha256': 'fixture'})
        self.assertEqual([entry['definition'] for entry in result['倓']['official_dictionary']], ['安靜。', '別音參見。'])
        self.assertEqual(result['倓']['official_dictionary'][0]['row'], 9)
        self.assertEqual(result['倓']['official_dictionary'][0]['source_url'], 'https://example.test/official.zip')
        self.assertNotIn('official_dictionary', result['铝'])
        self.assertEqual(result['云']['definition'], 'clouds')
        self.assertEqual(result['云']['official_dictionary'][0]['definition'], '說。')
        self.assertNotIn('definition', result['倓'])

    def test_existing_short_definition_does_not_hide_official_conflicting_sense(self):
        from scripts import build_character_definitions as builder
        result = builder.add_official_definitions(
            {'菉': {'definition': 'the green bamboo', 'mandarin': 'lù'}},
            [{'title': '菉', 'pinyin': 'lǜ', 'definition': '植物名。藎草之古稱。', 'row': 7},
             {'title': '菉', 'pinyin': 'lù', 'definition': '另一讀音。', 'row': 8}],
            {'source_url': 'https://example.test/official.zip', 'sha256': 'fixture'})
        self.assertEqual(result['菉']['definition'], 'the green bamboo')
        self.assertEqual([entry['pinyin'] for entry in result['菉']['official_dictionary']], ['lǜ', 'lù'])
        self.assertEqual(result['菉']['official_dictionary'][0]['definition'], '植物名。藎草之古稱。')

    def test_official_cross_reference_retains_the_actual_referred_entry(self):
        from scripts import build_character_definitions as builder
        records = [
            {'title': '棽', 'pinyin': 'shēn', 'definition': '參見「棽棽」條。', 'row': 8},
            {'title': '棽棽', 'pinyin': 'shēn shēn', 'definition': '繁盛的樣子。', 'row': 9},
            {'title': '棽麗', 'pinyin': 'shēn lì', 'definition': '不應自行關聯。', 'row': 10},
        ]
        result = builder.add_official_definitions({'棽': {}}, records, {'source_url': 'https://example.test/dict.zip', 'sha256': 'fixture'})
        entry = result['棽']['official_dictionary'][0]
        self.assertEqual([record['title'] for record in entry['cross_references']], ['棽棽'])
        self.assertEqual(entry['cross_references'][0]['definition'], '繁盛的樣子。')

    def test_conflicting_duplicate_cannot_overwrite_matching_reading(self):
        from scripts import build_character_definitions as builder
        result = builder.add_supplemental_definitions(
            {'瑆': {'mandarin': 'xīng'}, '冮': {'mandarin': 'gāng'}},
            [{'word': '瑆', 'pinyin': 'xīnɡ', 'explanation': '瑆xīng，玉的光彩。'},
             {'word': '瑆', 'pinyin': 'zhǎn', 'explanation': '瑆zhǎn，小杯子。'},
             {'word': '冮', 'pinyin': 'ɡānɡ', 'explanation': '冮gāng，姓。'}],
            {'source_url': 'https://example.test/dictionary', 'sha256': 'fixture'})
        self.assertEqual(result['瑆']['supplemental_dictionary']['explanation'], '瑆xīng，玉的光彩。')
        self.assertEqual(result['冮']['supplemental_dictionary']['explanation'], '冮gāng，姓。')

    def test_placeholders_are_not_usable_dictionary_evidence(self):
        from scripts import build_character_definitions as builder
        for placeholder in ('?', '？？', ' ... ', '待补充', '暂无解释'):
            with self.subTest(placeholder=placeholder):
                result = builder.add_supplemental_definitions(
                    {'芠': {}}, [{'word': '芠', 'explanation': placeholder}],
                    {'source_url': 'https://example.test/dictionary', 'sha256': 'fixture'})
                self.assertNotIn('supplemental_dictionary', result['芠'])

    def test_quarantined_source_entries_are_not_sent_as_meanings(self):
        from scripts import build_character_definitions as builder
        source = {'source_url': 'https://example.test/dictionary', 'sha256': 'fixture',
                  'excluded_entries': {'坬': {'reason': '释义与交叉核对来源冲突'}}}
        result = builder.add_supplemental_definitions(
            {'坬': {'mandarin': 'guà'}, '屾': {}},
            [{'word': '坬', 'explanation': '坬kuai，日本地名用字。'},
             {'word': '屾', 'explanation': '二山并立。'}], source)
        self.assertNotIn('supplemental_dictionary', result['坬'])
        self.assertIn('supplemental_dictionary', result['屾'])

    def test_supplemental_meanings_remain_separate_and_source_labeled(self):
        from scripts import build_character_definitions as builder
        records = [{'word': '屾', 'explanation': '屾shēn 1.二山并立。', 'more': '搜索相关成语'},
                   {'word': '佁', 'explanation': '佁：静止貌。', 'more': '佁然：静止的样子。'},
                   {'word': '冮', 'explanation': '', 'more': '搜索广告'}]
        data = {'屾': {'mandarin': 'shēn'}, '佁': {'mandarin': 'yǐ'}, '云': {'definition': 'clouds'}, '冮': {}}
        enriched = builder.add_supplemental_definitions(data, records, {'source_url': 'https://example.test/dictionary', 'sha256': 'fixture-hash'})
        self.assertNotIn('definition', enriched['屾'])
        self.assertEqual(enriched['屾']['supplemental_dictionary']['explanation'], '屾shēn 1.二山并立。')
        self.assertEqual(enriched['屾']['supplemental_dictionary']['source_url'], 'https://example.test/dictionary')
        self.assertEqual(enriched['屾']['supplemental_dictionary']['source_sha256'], 'fixture-hash')
        self.assertNotIn('搜索', str(enriched['屾']))
        self.assertNotIn('supplemental_dictionary', enriched['云'])
        self.assertNotIn('supplemental_dictionary', enriched['冮'])

    def test_traditional_senses_are_labeled_and_never_replace_missing_direct_definition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            readings = root / 'Unihan_Readings.txt'
            variants = root / 'Unihan_Variants.txt'
            readings.write_text('U+94DD\tkMandarin\tlǚ\nU+92C1\tkDefinition\taluminum\nU+2B1ED\tkMandarin\twěi\nU+853F\tkDefinition\tsurname; place name\nU+4E91\tkDefinition\tsay, speak; clouds\n')
            variants.write_text('U+94DD\tkTraditionalVariant\tU+92C1\nU+2B1ED\tkTraditionalVariant\tU+848D U+853F\n')
            result = build_definitions(list('铝𫇭云'), readings, variants)
        self.assertEqual(set(result), set('铝𫇭云'))
        self.assertNotIn('definition', result['铝'])
        self.assertEqual(result['铝']['related_characters'], [{'character': '鋁', 'relation': 'kTraditionalVariant', 'definition': 'aluminum'}])
        self.assertEqual(result['𫇭']['related_characters'][0], {'character': '蒍', 'relation': 'kTraditionalVariant'})
        self.assertEqual(result['云']['definition'], 'say, speak; clouds')
        self.assertNotIn('related_characters', result['云'])
