/**
 * The library explorer's tree, and the track viewer's field choices.
 *
 * A script for the same reason as the others here: there is no JS test runner. The tree's rules
 * about what is on screen are exactly the kind of thing that fails quietly - a filter that opens
 * nothing looks like a filter that found nothing, and a track list built for every album is the
 * 711ms freeze coming back without an error anywhere.
 *
 * Run it with:  node ui/test/tree.sim.cjs
 */

const { execFileSync } = require('child_process');
const fs = require('fs'), os = require('os'), path = require('path');

const UI = path.resolve(__dirname, '..');
const OUT = fs.mkdtempSync(path.join(os.tmpdir(), 'jimbrainz-tree-'));

execFileSync(path.join(UI, 'node_modules/.bin/tsc'), [
  'src/lib/libraryTree.ts', 'src/lib/trackFields.ts', '--outDir', OUT, '--module', 'commonjs',
  '--target', 'es2022', '--skipLibCheck', '--moduleResolution', 'node',
], { cwd: UI, stdio: 'inherit' });

//? under lib/ because the type-only imports pull src/api into the program, which roots it at src/
const tree = require(path.join(OUT, 'lib/libraryTree.js'));
const fields = require(path.join(OUT, 'lib/trackFields.js'));

let failures = 0;
function check(label, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) failures++;
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}: ${JSON.stringify(actual)}` +
              (ok ? '' : `  (expected ${JSON.stringify(expected)})`));
}

const track = (filename, title, disc = null, position = 1) => ({ filename, title, disc, position });
const album = (over) => ({
  path: over.path, artist: over.artist, album: over.album, edition: '', year: '', modified_at: 0,
  disc_count: 0, tracks: [], ...over,
});
const group = (editions) => ({
  key: `${editions[0].artist.toLowerCase()} ${editions[0].album.toLowerCase()}`,
  artist: editions[0].artist, album: editions[0].album, year: editions[0].year, yearRange: '',
  editions, needsAttention: 0, issues: [],
});

const wall = album({
  path: 'Pink Floyd/The Wall (1979)', artist: 'Pink Floyd', album: 'The Wall', year: '1979',
  disc_count: 2,
  tracks: [track('a', 'In the Flesh?', 1, 1), track('b', 'Mother', 1, 2),
           track('c', 'Hey You', 2, 1), track('d', 'Vera', 2, 2)],
});
const dsotm = album({
  path: 'Pink Floyd/DSOTM (1973)', artist: 'Pink Floyd', album: 'The Dark Side of the Moon',
  year: '1973', tracks: [track('e', 'Speak to Me'), track('f', 'Breathe')],
});
const standard = album({
  path: 'Tame Impala/The Slow Rush (2020)', artist: 'Tame Impala', album: 'The Slow Rush',
  year: '2020', tracks: [track('g', 'One More Year')],
});
const deluxe = album({
  path: 'Tame Impala/The Slow Rush (2020) [Deluxe]', artist: 'Tame Impala', album: 'The Slow Rush',
  edition: 'Deluxe', year: '2021', tracks: [track('h', 'One More Year'), track('i', 'Breathe Deeper')],
});
const undated = album({ path: 'aphex/x', artist: 'aphex twin', album: 'Untitled', tracks: [track('j', 'Xtal')] });

const groups = [group([wall]), group([dsotm]), group([standard, deluxe]), group([undated])];
const artists = tree.groupArtists(groups, 'name', () => false);

const state = (over = {}) => ({
  expanded: new Set(), closedWhileFiltering: new Set(), filtering: false, trackMatches: new Set(), ...over,
});
const shape = (rows) => rows.map((r) => `${r.kind}${r.level}:${
  r.kind === 'artist' ? r.node.artist : r.kind === 'group' ? r.group.album
  : r.kind === 'edition' ? r.album.edition || 'Standard' : r.kind === 'disc' ? r.disc : r.track.title}`);

console.log('\nordering');
check('artists A-Z, case-insensitively', artists.map((a) => a.artist), ['aphex twin', 'Pink Floyd', 'Tame Impala']);
check("an artist's albums run by year", artists[1].groups.map((g) => g.album), ['The Dark Side of the Moon', 'The Wall']);

console.log('\nnothing exists below a closed node');
check('closed: only artists', shape(tree.visibleRows(artists, state())),
      ['artist1:aphex twin', 'artist1:Pink Floyd', 'artist1:Tame Impala']);
check('no track row anywhere until an album is opened',
      tree.visibleRows(artists, state({ expanded: new Set([tree.artistNodeId('Pink Floyd')]) }))
        .filter((r) => r.kind === 'track').length, 0);

console.log('\nopening walks down a level at a time');
const floydOpen = state({ expanded: new Set([tree.artistNodeId('Pink Floyd'), tree.groupNodeId(groups[0])]) });
//? from 2: aphex twin and Pink Floyd themselves come first
check('a multi-disc album splits by disc', shape(tree.visibleRows(artists, floydOpen)).slice(2, 10), [
  'group2:The Dark Side of the Moon', 'group2:The Wall',
  'disc3:1', 'track3:In the Flesh?', 'track3:Mother', 'disc3:2', 'track3:Hey You', 'track3:Vera',
]);
const tameOpen = state({ expanded: new Set([tree.artistNodeId('Tame Impala'), tree.groupNodeId(groups[2])]) });
check('an album with several editions opens to its editions, not its tracks',
      shape(tree.visibleRows(artists, tameOpen)).filter((s) => !s.startsWith('artist')),
      ['group2:The Slow Rush', 'edition3:Standard', 'edition3:Deluxe']);

console.log('\nfiltering opens what it found');
check('artists open by themselves while filtering',
      shape(tree.visibleRows(artists, state({ filtering: true }))).filter((s) => s.startsWith('group')).length, 4);
check('...unless you closed one again',
      shape(tree.visibleRows(artists, state({
        filtering: true, closedWhileFiltering: new Set([tree.artistNodeId('Pink Floyd')]),
      }))).filter((s) => s.startsWith('group')).length, 2);

const heyYou = tree.trackNodeId(wall, wall.tracks[2]);
const matched = tree.visibleRows(artists, state({ filtering: true, trackMatches: new Set([heyYou]) }));
check('a song match opens its album and shows just that song',
      shape(matched).filter((s) => s.startsWith('track') || s.startsWith('disc')), ['disc3:2', 'track3:Hey You']);
check('and marks it as the match', matched.find((r) => r.id === heyYou)?.match, true);
check('opening that album by hand shows all of it again',
      tree.visibleRows(artists, state({
        filtering: true, trackMatches: new Set([heyYou]), expanded: new Set([tree.groupNodeId(groups[0])]),
      })).filter((r) => r.kind === 'track' && r.album === wall).length, 4);

console.log('\nwhat a node is, and what must open to reach it');
const index = tree.indexTree(artists);
check('a single-edition album IS its release', index.get(tree.groupNodeId(groups[1]))?.kind, 'album');
check('a multi-edition album is the group as a whole', index.get(tree.groupNodeId(groups[2]))?.kind, 'group');
check('a track in an edition needs artist, album and edition open',
      tree.ancestorsOf(tree.trackNodeId(deluxe, deluxe.tracks[1]), artists),
      [tree.artistNodeId('Tame Impala'), tree.groupNodeId(groups[2]), tree.editionNodeId(deluxe)]);
check('a track on a single-edition album needs artist and album',
      tree.ancestorsOf(heyYou, artists), [tree.artistNodeId('Pink Floyd'), tree.groupNodeId(groups[0])]);

console.log('\nfield choices outlive the version that wrote them');
const initial = fields.TRACK_FIELDS.filter((f) => f.initial).map((f) => f.id);
check('nothing saved: the defaults', fields.reconcileVisible(null), initial);
check('a removed field is dropped, not kept as a dead column',
      fields.reconcileVisible({ visible: ['number', 'gone'], seen: ['number', 'gone'] }).includes('gone'), false);
const everyId = fields.TRACK_FIELDS.map((f) => f.id);
check('a field you turned off stays off',
      fields.reconcileVisible({ visible: ['number'], seen: everyId }), ['number']);
check('a field added since you chose takes its own default rather than staying hidden',
      fields.reconcileVisible({ visible: ['number'], seen: everyId.filter((id) => id !== 'bitrate') }),
      ['number', 'bitrate']);

console.log(failures ? `\n${failures} FAILED\n` : '\nall passed\n');
process.exit(failures ? 1 : 0);
