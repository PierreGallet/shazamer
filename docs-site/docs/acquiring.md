---
id: acquiring
title: Getting the record
---

# Getting the record

Every identified track has somewhere to go and get it.

## Stores first

Bandcamp, Beatport and Discogs come first in the list, and not as a
disclaimer. A purchased file arrives with correct tags and a catalogue number,
and it pays the label you have just discovered.

The catalogue number that enrichment found is the useful thing to search with —
more precise than a title, which several records may share.

## Soulseek

Off unless you configure it. It needs [slskd](https://github.com/slskd/slskd),
your own Soulseek account, and a shared folder in return.

Two facts about the network that are not optional:

- **One session per account.** Running slskd on a server while a desktop client
  is logged in with the same credentials makes the two evict each other in a
  loop. Give the server its own account.
- **It must share something.** A client that only takes is throttled and
  eventually refused; peers check. Here the share is the downloads directory —
  you offer back what you have taken — which is why downloads are kept for six
  months rather than the fortnight set audio gets.

### Choosing a candidate

Pressing **Get** searches and shows the best five rather than picking blind,
because the difference that matters most is invisible until someone looks.

Candidates are ranked on four things:

| | Weight | Why |
| --- | --- | --- |
| **Length** | −60 to +50 | The largest single factor. A radio edit has no intro to mix into and no outro to mix out of. |
| **Format** | 20 to 100 | Depends on the profile — see below |
| **Filename** | 0 to +60 | Fuzzy match against artist and title. The main guard against something unrelated. |
| **Availability** | −25 to +25 | A perfect file behind forty people is worse than a good one now |

Length dominates on purpose, and the preference is **absolute** rather than
matched against the identified track. Shazam identifies whichever recording it
matched, which for a lot of dance records is the radio edit — so treating its
duration as a target would systematically reject the extended mix.

The result: a radio edit in FLAC ranks far below an extended mix in MP3.

### Format profiles

`ACQUIRE_FORMAT_PROFILE` decides what "best" means, because it depends on where
the file is going.

| Profile | Order | For |
| --- | --- | --- |
| `portable` *(default)* | MP3, AAC, then lossless | Plays everywhere. FLAC does not import into Apple Music, and is roughly three times the size — 40 MB against 14 for six minutes. |
| `lossless` | FLAC, WAV, then lossy | Rekordbox, Serato and Traktor read FLAC natively |
| `apple` | ALAC, AAC, MP3, FLAC last | An Apple Music library |

### What happens after the download

**It is verified.** Filenames on Soulseek are whatever the uploader typed —
mislabelled rips, wrong versions and occasionally a different song entirely. The
file is fingerprinted with the same identifier that found the track, and one
that comes back as something else is refused, naming what it actually is. A
wrong record filed under the right name is worse than no record: you find out
at the decks.

The window is taken from partway in, not the opening. Intros are quiet,
sometimes silent, and a fingerprint of silence identifies nothing.

**It is tagged**, with the metadata already established, so it arrives correct
rather than as whatever the uploader typed.

**It is served to your browser** and swept from the server later. The server is
how the file reaches you, not where it lives.
