#!/usr/bin/env python3
"""Build sourced direct definitions and explicitly labeled variant context."""
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def add_official_definitions(definitions, records, source):
    result = {character: dict(entry) for character, entry in definitions.items()}
    by_title = {}
    for record in records:
        by_title.setdefault(record.get('title'), []).append(record)
    for record in records:
        character = record.get('title')
        # Unihan's short gloss is not exhaustive and can disagree with a full
        # entry. Keep both sources visible instead of hiding the latter.
        if character not in result or not record.get('definition'):
            continue
        entry = {
            **record, 'source_url': source['source_url'], 'source_sha256': source['sha256'],
            'authority': 'Ministry of Education Revised Mandarin Chinese Dictionary',
        }
        references = re.findall(r'「([^」]+)」', record['definition']) if '參見' in record['definition'] else []
        related = [dict(item) for title in dict.fromkeys(references)
                   if title != character for item in by_title.get(title, [])]
        if related:
            entry['cross_references'] = related
        result[character].setdefault('official_dictionary', []).append(entry)
    return result


def add_supplemental_definitions(definitions, records, source):
    result = {character: dict(entry) for character, entry in definitions.items()}
    for record in records:
        character = record.get('word')
        explanation = record.get('explanation', '').strip()
        if (character not in result or 'definition' in result[character]
                or not explanation or not re.search(r'[\u3400-\u9fff]', explanation)
                or explanation in {'待补充', '暂无解释', '暂无释义'}
                or character in source.get('excluded_entries', {})):
            continue
        reading = record.get('pinyin', '').strip().replace('ɡ', 'g')
        known_readings = result[character].get('mandarin', '').split()
        if reading and known_readings and reading not in known_readings:
            result[character].setdefault('quarantined_supplemental_records', []).append({
                'reading': reading, 'explanation': explanation,
                'reason': 'reading_conflicts_with_unihan_requires_cross_check',
                'source_url': source['source_url'], 'source_sha256': source['sha256'],
            })
            continue
        result[character]['supplemental_dictionary'] = {
            'explanation': explanation,
            'source_url': source['source_url'],
            'source_sha256': source['sha256'],
            'authority': 'third_party_compilation_requires_cross_check',
        }
    return result


def build_definitions(characters, readings_path, variants_path):
    readings = {}
    fields = {'kDefinition': 'definition', 'kMandarin': 'mandarin'}
    for line in readings_path.read_text(encoding='utf-8').splitlines():
        if not line or line.startswith('#'):
            continue
        code, field, value = line.split(chr(9), 2)
        if field in fields:
            character = chr(int(code[2:], 16))
            readings.setdefault(character, {})[fields[field]] = value
    allowed = set(characters)
    result = {character: dict(readings.get(character, {})) for character in characters}
    for line in variants_path.read_text(encoding='utf-8').splitlines():
        if not line or line.startswith('#'):
            continue
        code, relation, value = line.split(chr(9), 2)
        character = chr(int(code[2:], 16))
        if character not in allowed or relation != 'kTraditionalVariant':
            continue
        result[character]['related_characters'] = [
            {'character': related, 'relation': relation, **readings.get(related, {})}
            for related in (chr(int(token.split('<')[0][2:], 16)) for token in value.split())
        ]
    return result


if __name__ == '__main__':
    source_dir = ROOT / '参考文献' / '字义数据' / 'Unicode-17.0.0'
    source = json.loads((source_dir / 'source.json').read_text())
    for filename, expected in source['extracted_files'].items():
        actual = hashlib.sha256((source_dir / filename).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError('Source hash mismatch: ' + filename)
    characters = json.loads((ROOT / 'data' / 'characters-8105.json').read_text())
    source['characters'] = build_definitions(characters, source_dir / 'Unihan_Readings.txt', source_dir / 'Unihan_Variants.txt')
    supplemental_dir = source_dir.parent / 'chinese-xinhua'
    supplemental_source = json.loads((supplemental_dir / 'source.json').read_text())
    supplemental_data = (supplemental_dir / 'word.json').read_bytes()
    if hashlib.sha256(supplemental_data).hexdigest() != supplemental_source['sha256']:
        raise ValueError('Supplemental source hash mismatch')
    source['characters'] = add_supplemental_definitions(source['characters'], json.loads(supplemental_data), supplemental_source)
    # Conflicting records remain in a separate audit file, never in model context.
    quarantine = {ch: entry.pop('quarantined_supplemental_records')
                  for ch, entry in source['characters'].items()
                  if 'quarantined_supplemental_records' in entry}
    (supplemental_dir / 'quarantined-records.json').write_text(
        json.dumps(quarantine, ensure_ascii=False, indent=2) + chr(10))
    official_dir = source_dir.parent / 'moe-revised-2015_20260929'
    official_source = json.loads((official_dir / 'source.json').read_text())
    official_data = (official_dir / 'entries.json').read_bytes()
    if hashlib.sha256(official_data).hexdigest() != official_source['entries_sha256']:
        raise ValueError('Official dictionary source hash mismatch')
    source['characters'] = add_official_definitions(source['characters'], json.loads(official_data), official_source)
    source['notice'] = 'Direct senses are never inferred. related_characters preserves kTraditionalVariant links and the linked character own direct fields; equivalence of every sense is not assumed. Supplemental records with conflicting readings are quarantined pending cross-check; this does not prove those readings invalid.'
    path = ROOT / 'data' / 'character_definitions.json'
    path.write_text(json.dumps(source, ensure_ascii=False, indent=2) + chr(10))
    print(json.dumps({'characters': len(characters), 'direct_definitions': sum('definition' in e for e in source['characters'].values()), 'with_related_context': sum('related_characters' in e for e in source['characters'].values()), 'supplemental_definitions': sum('supplemental_dictionary' in e for e in source['characters'].values())}))
