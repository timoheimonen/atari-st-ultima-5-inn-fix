# Ultima V: Warriors of Destiny (Atari ST) – inn price fix

A patch for the Atari ST boot disk image `Ultima V - Warriors of Destiny (Disk 1 of 3).STX`
(Pasti STX) that fixes the shop prices jumping to the maximum after a companion
has been left at an inn.

## What was found

The Atari ST version has a long-known bug report: *leaving a companion at an inn
drops the party's karma to the minimum, which pushes all shop prices to the
maximum.* The price jump is real, but karma has nothing to do with it:

- Leaving a companion does not change karma. Merchant prices do not use karma at
  all. They depend on the intelligence of the party member who does the buying:
  `price = base + base * (100 - 3 * INT) / 100`, from 1.1 × base (INT 30) to
  2 × base (INT 0).
- The real bug is in the inn's "Leave a companion". When a member leaves, the game
  keeps the *active party member* index in step: if the active member came after
  the one who left, the index is decremented. The comparison is unsigned, so the
  normal value `$FF` ("no active member") is decremented too and becomes `$FE`.
  Every further companion left at an inn decrements it again (`$FD`, `$FC`, …).
- Shops use the active member as the buyer unless the index is `$FF`. With `$FE`
  they take member #254, whose record lies outside the party roster. Its
  intelligence reads as 0, so every price becomes 2 × base, the maximum.
- The index is part of the saved game, so the high prices survive saving and
  loading. The status display shows no active member either way, so nothing
  on screen gives it away.

Measured in Hatari (1040 ST, TOS 1.04) with a game in progress: leaving Mariah at
the North Britanny inn changed the index from `$FF` to `$FE`; a horse at the North
Britanny stables then cost 260 gp instead of 170 gp. Karma stayed at 79.

**Already affected saved games:** press `0` once in the game ("Set Active Plr:
None"). This sets the index back to `$FF` and the prices return to normal. It
works with the original and the fixed program, so no save game editing is needed.

## How it was fixed

One 6-byte patch in `AUTO\U5.PRG` (68000 source: [src/inn_fix.s](src/inn_fix.s)),
at text offset `$36D9E`:

| | Code | Bytes |
| --- | --- | --- |
| Original | `cmp.l (-10,a6),d0` / `bls.s` | `B0 AE FF F6 63 08` |
| Fixed | `cmp.b (-7,a6),d0` / `ble.s` | `B0 2E FF F9 6F 08` |

The low bytes are compared as signed values, so `$FF` (−1) is never larger than a
member index, and the indices 0–15 compare as before. The patch keeps the size of
the code, has no absolute address and leaves the relocation table as it is. The
patched program is written back into the same sector of the STX image in place;
3 bytes of the image change.

Checked in Hatari with the fixed disk: after leaving Mariah at the inn the
index stays `$FF`, and the party, the room price and karma are as before.

## The disk prompt hang

The ST version is also known for asking for the disk that is already in the
drive and then hanging. What the program does:

- Every file read or write retries until the file opens. If the open fails, the
  game asks again for the disk it is using ("Please insert Play/Underground
  Disk…"), even though that disk is in the drive.
- The prompt checks the disk by opening a different file, which succeeds, and
  the original open is tried again. If it keeps failing, the prompt comes back
  forever. Esc is not handled there; only a reset gets out.

So any lasting failure to open a file turns into this endless prompt. One sure
way to trigger it is a **write-protected Play/Underground disk**: the game writes
to it when saving and when entering a dungeon. Keep that disk writable.

After swapping a disk, wait a couple of seconds before pressing the key. If the
key comes too soon, TOS may not notice the disk change and the game says "Wrong
Disk"; pressing the key again is enough.

The random hang in the second half of the game that players reported could not
be reproduced: about 400 disk swaps on TOS 1.04 (saving inside a dungeon over and
over) ran without a single failed open. The cause on the machines of the time is
therefore not known, and this patch does not change the disk handling.

## Usage

```sh
python3 patch.py "Ultima V - Warriors of Destiny (Disk 1 of 3).STX"   # writes ...-fixed.stx next to the input
```

The STX disk images are available at
[Atarimania](https://www.atarimania.com/games/atari-st-games-ultima-v-warriors-of-destiny-10735).
Only the image with SHA-256
`8ea59c11c3c862f3fdcbc83f9ad2af0eda175df0918523d573bdb9f0b8f39c1d` is accepted.
The result has SHA-256
`cae03bb83c04a77355343fb93d107a5895c264acb141affb09201f8e5f7a9a32`.
Python 3 is the only requirement. If `vasmm68k_mot` is installed, `patch.py` also
checks that `src/inn_fix.s` assembles to the same bytes.

Only the boot disk changes; use disks 2 and 3 as they are. The save game format
is unchanged.

No game files are included. Ultima V: Warriors of Destiny is © 1988 Origin
Systems / Electronic Arts.

## License

MIT. © 2026 Timo Heimonen <timo.heimonen@proton.me>. See [LICENSE](LICENSE).
