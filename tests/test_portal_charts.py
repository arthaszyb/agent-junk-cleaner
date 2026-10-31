"""The chart helpers that need no browser (scale.mjs, icons.mjs), and the grey ladder the charts and the tier pills share."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from disk_steward import i18n

STATIC = Path(i18n.__file__).resolve().parent.parent / 'portal' / 'static'
NODE = shutil.which('node')


def node_json(script):
    """Run an ES-module snippet in node (cwd: the static folder) and return what it printed as JSON."""
    r = subprocess.run([NODE, '--input-type=module', '-e', script], capture_output=True, text=True, cwd=STATIC)
    if r.returncode:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout)


@unittest.skipUnless(NODE, 'node is not installed')
class ScaleTests(unittest.TestCase):
    def test_byte_ticks_are_round_numbers_in_binary_units(self):
        out = node_json("""import { byteTicks } from './scale.mjs';
const G = 1024 ** 3;
console.log(JSON.stringify([byteTicks(0, 37 * G, { zero: true }), byteTicks(800 * G, 830 * G), byteTicks(0, 3000, { zero: true })]));""")
        labels = [[t['label'] for t in axis['ticks']] for axis in out]
        self.assertEqual(labels[0], ['0', '10 GiB', '20 GiB', '30 GiB', '40 GiB'])
        self.assertEqual(labels[1], ['800 GiB', '810 GiB', '820 GiB', '830 GiB'])
        self.assertEqual(labels[2], ['0', '1 KiB', '2 KiB', '3 KiB'])
        self.assertEqual(out[0]['max'], 40 * 1024 ** 3)   # the axis reaches the last tick, so no mark is cut off

    def test_a_flat_series_still_gets_an_axis(self):
        out = node_json("""import { byteTicks, niceTicks } from './scale.mjs';
console.log(JSON.stringify([niceTicks(5, 5).ticks, byteTicks(10 * 1024 ** 3, 10 * 1024 ** 3).ticks.length]));""")
        self.assertGreaterEqual(len(out[0]), 2)
        self.assertGreaterEqual(out[1], 2)

    def test_segments_fill_the_width_with_exact_gaps(self):
        out = node_json("""import { segmentLayout } from './scale.mjs';
console.log(JSON.stringify([segmentLayout([10, 0, 30, 60], 100, 2), segmentLayout([0, 0], 100, 2), segmentLayout([1], 50, 2)]));""")
        live = [s for s in out[0] if s['w'] > 0]
        self.assertEqual(len(live), 3)
        self.assertAlmostEqual(sum(s['w'] for s in live) + 2 * (len(live) - 1), 100)
        for left, right in zip(live, live[1:]):
            self.assertAlmostEqual(right['x'] - (left['x'] + left['w']), 2)   # 2px of surface between neighbours
        self.assertAlmostEqual(live[0]['w'] / live[1]['w'], 1 / 3)
        self.assertEqual([s['w'] for s in out[1]], [0, 0])
        self.assertEqual(out[2][0]['w'], 50)

    def test_rounded_rect_rounds_only_the_asked_corners_and_never_past_half(self):
        out = node_json("""import { roundedRect } from './scale.mjs';
console.log(JSON.stringify([roundedRect(0, 0, 20, 10, { tl: 4, tr: 4 }), roundedRect(0, 0, 20, 4, { tl: 4, tr: 4 }), roundedRect(0, 0, 20, 10, {})]));""")
        self.assertEqual(out[0].count('A'), 2)
        self.assertTrue(out[0].startswith('M4 0H16A4 4'))
        self.assertIn('A2 2', out[1])        # a 4px tall bar cannot take a 4px radius
        self.assertNotIn('A', out[2])

    def test_time_ticks_are_inside_the_range_and_not_crowded(self):
        out = node_json("""import { timeTicks, timeLabel } from './scale.mjs';
const t0 = 1761000000;
console.log(JSON.stringify([timeTicks(t0, t0 + 600), timeTicks(t0, t0 + 30 * 86400), timeTicks(t0, t0 + 400 * 86400), timeLabel(t0, 600), timeLabel(t0, 30 * 86400)]));""")
        for ticks, (t0, t1) in zip(out[:3], [(0, 600), (0, 30 * 86400), (0, 400 * 86400)]):
            base = 1761000000
            self.assertTrue(all(base + t0 - 1 <= t <= base + t1 + 1 for t in ticks['ticks']))
            self.assertLessEqual(len(ticks['ticks']), 12)
        self.assertRegex(out[3], r'^\d\d:\d\d$')
        self.assertRegex(out[4], r'^\d\d-\d\d$')

    def test_disk_level_matches_the_words_the_pages_use(self):
        out = node_json("""import { diskLevel } from './scale.mjs';
console.log(JSON.stringify([diskLevel(50, 100), diskLevel(80, 100), diskLevel(89, 100), diskLevel(90, 100), diskLevel(1, 0)]));""")
        self.assertEqual(out, ['ok', 'warn', 'warn', 'crit', 'ok'])

    def test_nearest_picks_the_closest_position(self):
        out = node_json("""import { nearest } from './scale.mjs';
console.log(JSON.stringify([nearest([0, 10, 50], 12), nearest([0, 10, 50], 40), nearest([5], -100)]));""")
        self.assertEqual(out, [1, 2, 0])


@unittest.skipUnless(NODE, 'node is not installed')
class IconTests(unittest.TestCase):
    def names(self):
        return set(node_json("import { ICON_NAMES } from './icons.mjs';\nconsole.log(JSON.stringify(ICON_NAMES));"))

    def test_the_set_has_every_icon_the_design_lists(self):
        wanted = {'overview', 'insights', 'browse', 'cleanup', 'archive', 'advisor', 'settings', 'scan', 'check', 'alert', 'info', 'lock',
                  'external', 'copy', 'chevron', 'sun', 'moon', 'menu', 'close'}
        self.assertLessEqual(wanted, self.names())

    def test_every_icon_a_module_names_exists(self):
        used = set()
        for path in STATIC.rglob('*.mjs'):
            code = path.read_text(encoding='utf-8')
            used |= set(re.findall(r"\bicon\('([a-z_]+)'", code))
            used |= set(re.findall(r"iconName:\s*'([a-z_]+)'", code))
            for table in re.findall(r'const (?:TIER_ICON|SEVERITY_ICON|TOAST_ICON|BADGE_ICON) = \{(.*?)\};', code, flags=re.S):
                used |= set(re.findall(r":\s*'([a-z_]+)'", table))
            if path.name == 'shell.mjs':   # the NAV and THEMES pairs: [name, icon]
                used |= set(re.findall(r"\['[a-z_]+', '([a-z_]+)'\]", code))
        self.assertGreater(len(used), 15)
        self.assertEqual(sorted(used - self.names()), [])


def hex_to_oklab(hexcode):
    """OKLab (L, a, b) of an sRGB colour (Ottosson)."""
    r, g, b = (int(hexcode[i:i + 2], 16) / 255 for i in (1, 3, 5))

    def lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = lin(r), lin(g), lin(b)
    l_ = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m_ = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s_ = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
            1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
            0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_)


def delta_e(a, b):
    """OKLab distance times 100: what the palette validator calls normal-vision ΔE."""
    return 100 * sum((x - y) ** 2 for x, y in zip(hex_to_oklab(a), hex_to_oklab(b))) ** 0.5


class EntityColorTests(unittest.TestCase):
    """Same entity, same colour: the bars, the columns, the legends and the dots read these tokens. In the order the
    composition bar draws them, neighbours must stay apart."""
    ORDER = ('--c-junk', '--c-review', '--c-report_only', '--c-protected', '--c-outside', '--c-free')

    def palette(self, scope):
        css = (STATIC / 'styles.css').read_text(encoding='utf-8')
        block = css[css.index('{', css.index(scope)):css.index('--c-agent', css.index(scope))]
        return [re.search(re.escape(name) + r':\s*(#[0-9a-fA-F]{6})', block).group(1) for name in self.ORDER]

    def test_adjacent_colours_differ_by_at_least_15_oklab_points_in_both_themes(self):
        for scope in (':root {', ':root[data-theme="dark"] {'):
            colours = self.palette(scope)
            with self.subTest(scope=scope, palette=colours):
                for a, b in zip(colours, colours[1:]):
                    self.assertGreaterEqual(delta_e(a, b), 15 - 0.6, f'{a} and {b}')   # the validator rounds ΔE to one place

    def test_the_bar_draws_them_in_this_order(self):
        src = (STATIC / 'views' / 'insights.mjs').read_text(encoding='utf-8')
        self.assertIn("const DRAW_ORDER = ['junk', 'review', 'report_only', 'protected', 'outside', 'free'];", src)


if __name__ == '__main__':
    unittest.main()
