# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0](https://github.com/PierreGallet/shazamer/compare/v0.2.0...v0.3.0) (2025-12-30)


### Features

* download from url soundcloud or youtube directly ([cd209e3](https://github.com/PierreGallet/shazamer/commit/cd209e39815f0bfea5d48927d044d276b00e3c3b))
* small visual adjustments on front end and fix view mode ([0095837](https://github.com/PierreGallet/shazamer/commit/0095837ebc89486beb6d524a196aa63154d6a2bb))


### Bug Fixes

* fix for youtube urls ([49dce54](https://github.com/PierreGallet/shazamer/commit/49dce54c7bbf5fe5472fb5726edbbad53e1d7d9e))
* still youtube url fix ([f471d5d](https://github.com/PierreGallet/shazamer/commit/f471d5d73a684d86fc318c89a3fd998d692a93c2))
* update yt-dlp download to use remote components for JS challenge solving ([ecdc276](https://github.com/PierreGallet/shazamer/commit/ecdc27606259b032cf79f9439cc9bb91d89f4f85))
* upgrade version ([e23ebcc](https://github.com/PierreGallet/shazamer/commit/e23ebcc2cf34061736d8404ef833b5a3b1e9fa62))

## [0.2.0](https://github.com/PierreGallet/shazamer/compare/v0.1.0...v0.2.0) (2025-12-29)


### Features

* add simple deployment script ([0061bf9](https://github.com/PierreGallet/shazamer/commit/0061bf9e6f2f5958fba8e0b2706c879648a38b57))
* added a front end for easier usage ([6e8c18d](https://github.com/PierreGallet/shazamer/commit/6e8c18d019fa0299a84838d5aa0af7cb4620f910))
* switch to uv ([1f4c2d1](https://github.com/PierreGallet/shazamer/commit/1f4c2d113c51a3837c147c1230946fd9e821e38b))

## 0.1.0 (2025-12-27)


### Features

* add automatic parameter adjustment based on audio duration ([7ac6e85](https://github.com/PierreGallet/shazamer/commit/7ac6e8514d4ba4b6ac4964f82649c27a95f0a38d))
* add release-please workflow and improve temp file handling ([283e8b4](https://github.com/PierreGallet/shazamer/commit/283e8b4853d4951414cecd1c5d1aef74d270a22c))


### Bug Fixes

* remove deprecated package-name parameter from release-please ([a5ab693](https://github.com/PierreGallet/shazamer/commit/a5ab693e7fea91a203bc956148977a45ce2bc4ce))
* use personal access token for release-please workflow ([5aaf4db](https://github.com/PierreGallet/shazamer/commit/5aaf4dbc53edb3115bbf33c881b3ee0eedc0a9a7))

## [1.0.0] - 2024-12-27

### Features

- Automatic track detection using spectral analysis
- Shazam integration for track identification
- JSON and TXT output formats with timestamps
- Confidence scoring based on match count
- Customizable detection parameters (threshold, minimum song duration)
- Make commands for easy usage
- Automatic output file naming based on input filename
- Debug mode for troubleshooting

### Initial Release

First public release of Shazamer - a tool to automatically identify tracks in DJ sets and long audio mixes.
