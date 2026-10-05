# Thumb Buddy — Test Media Naming Scheme

## Directory Structure

```
/test-media
  /action-sports
  /animation
  /high-motion
  /landscape
  /low-light
  /negative
  /talking-head
  /tutorial
```

## Filename Pattern

```
<slug>__<res>_<orient>_<fps>_<codec>_<depth>_<duration>[_<extras>].<ext>
```

The double underscore (`__`) separates the free-form description from the metadata block. The slug may contain hyphens and words freely; everything after `__` splits cleanly on single underscores in a fixed field order.

General rules:

- Lowercase only, no spaces.
- Words within the slug and within extras are joined with hyphens.
- Field order is fixed. A field whose value can't be determined is omitted; there is no placeholder. Each field has a distinct shape (`…p`, `land`/`port`/`sq`, `…fps`, `…bit`, `…s`/`…m…s`/`…h…m`), so the remaining fields are still identified unambiguously.
- `res`, `orient` and `codec` always appear together: they are present whenever the file has a readable video stream and absent otherwise.
- A file with no readable video stream keeps only its duration (`<slug>__<duration>.<ext>`). A file with nothing readable keeps only its slug (`<slug>.<ext>`).

## Examples

```
/action-sports/skatepark-kickflip__2160p_land_59.94fps_hevc_10bit_42s_hdr-hlg.mov
/talking-head/podcast-two-shot__1080p_land_29.97fps_h264_8bit_12m05s.mp4
/tutorial/screen-recording-xcode__1440p_land_60fps_h264_8bit_3m30s_vfr.mp4
/low-light/candlelit-dinner__1080p_port_30fps_hevc_10bit_18s_rot90.mov
/landscape/drone-coastline__2160p_land_23.976fps_prores422_10bit_1m12s.mov
/negative/truncated-moov-atom__1080p_land_30fps_h264_8bit.mp4
/negative/audio-only__10s.mp3
/negative/zero-byte.mp4
```

## Token Vocabulary

| Field | Values | Notes |
|---|---|---|
| `res` | `480p`, `720p`, `1080p`, `1440p`, `2160p`, `4320p` | Based on the short edge, so portrait 1080×1920 is still `1080p`. Use exact `WxH` (e.g. `1080x1350`) for non-standard sizes. |
| `orient` | `land`, `port`, `sq` | Display orientation, not stored orientation (see `rot` extras). |
| `fps` | `23.976fps`, `25fps`, `29.97fps`, `59.94fps`, `120fps` | Keep the real fractional rate; it matters for seek accuracy. |
| `codec` | `h264`, `hevc`, `av1`, `vp9`, `prores422`, `mpeg2` | Match ffprobe's `codec_name` where possible for programmatic comparison. |
| `depth` | `8bit`, `10bit`, `12bit` | |
| `duration` | `42s`, `12m05s`, `1h02m` | Seconds only under a minute, minutes and seconds under an hour, hours and minutes beyond. Omitted when the file is broken or duration is unreadable. |
| `extras` | `hdr-pq`, `hdr-hlg`, `vfr`, `rot90`, `rot270`, `422`, `444`, `noaudio`, `anamorphic` | Optional. Join multiple with hyphens (e.g. `hdr-pq-vfr`). |

## Notes for a Thumbnail Engine

**Rotation metadata.** A phone clip stored as 1920×1080 with a 90° display matrix is one of the most common causes of sideways thumbnails. Tag these as `port_..._rot90` so the test case is explicit.

**Variable frame rate.** Screen recordings and phone footage often use VFR, which can break timestamp-based seeking. Flag them with `vfr`.

**Negative cases.** In `/negative`, the slug names the defect (`truncated-moov-atom`, `zero-byte`, `audio-only`, `wrong-extension`). Metadata that can't be known is omitted.

## Parsing Regex

```regex
^(?P<slug>[a-z0-9-]+)(?:__(?=[a-z0-9])(?:(?P<res>\d{3,4}p|\d+x\d+)_(?P<orient>land|port|sq)(?:_(?P<fps>\d+(?:\.\d+)?fps))?_(?P<codec>[a-z0-9]+)(?:_(?P<depth>\d+bit))?)?(?:_?(?P<duration>\d+h\d{2}m|\d+m\d{2}s|\d{1,2}s))?(?:_(?P<extras>[a-z0-9-]+))?)?\.(?P<ext>[a-z0-9]+)$
```

Extras should be split against the known token list rather than on hyphens alone, since some tokens (`hdr-pq`, `hdr-hlg`) contain hyphens themselves.

## Keeping Names Honest

Generate the metadata block from `ffprobe` with a rename script rather than typing it by hand, and have the test suite assert that probed values match the filename. The name then serves as both documentation and a lightweight fixture check.
