# Notice

## Firefly marks

The Firefly name, the firefly wordmark with its spark and chevron, the slogan
"Attitude makes the difference." and the brand's graphic systems are
proprietary to Firefly Software Solutions Inc. All rights reserved.

This kit is for internal use and for partners and suppliers working on
Firefly's behalf with the company's approval. Don't modify the marks, use them
to suggest an endorsement or partnership, or redistribute them outside that
purpose.

The source code in this repository is also proprietary and is not licensed
for use outside Firefly Software Solutions Inc.

## Manrope

The kit uses the Manrope typeface, copyright 2019 The Manrope Project Authors
(https://github.com/sharanda/manrope), licensed under the SIL Open Font
License, Version 1.1. The licence text ships with the fonts in
`04-Typography/fonts/OFL.txt` in the kit, and is installed with the
`@fontsource/manrope` package
(`node_modules/@fontsource/manrope/LICENSE`).

The OFL lets you use, study, modify and redistribute the fonts, including
with the kit, as long as they are not sold on their own and modified versions
are renamed.

## Icons

The Firefly icon system (`icons/` in this repository, `14-Icons/` in the kit)
combines icons drawn for Firefly with icons derived from Lucide
(https://lucide.dev), copyright 2026 Lucide Icons and Contributors, licensed
under the ISC License. Some Lucide icons derive from Feather, copyright
2013-present Cole Bemis, licensed under the MIT License. Firefly redraws the
Lucide icons with a 1.5 stroke; their geometry is Lucide's.

The full licence text, with the list of icons that come from Lucide, ships in
`icons/LICENSE` and in `14-Icons/LICENSE` in the kit. Keep it with any copy of
the icons. The icons drawn for Firefly (`icons/custom/`) are proprietary to
Firefly Software Solutions Inc., like the rest of the brand's graphic systems.

The ISC and MIT licences let you use, copy, modify and distribute the Lucide
icons, including in commercial work, provided the copyright notice and the
licence text are kept with them.

## Enterprise systems library

`systems/` holds a catalogue of third-party vendors, platforms and enterprise
systems with their logos, used to identify those systems in Firefly's client
deliverables (diagrams, decks and documents). It is internal and the
repository is private.

- Vendor names, logos and product names are trademarks of their owners and
  belong to them. Firefly uses them only to identify the systems they name
  (nominative use), never to suggest a partnership, certification or
  endorsement that does not exist.
- The library is never used on the public website, which shows generic
  categories only, and never ships in the public brand kit.
- Don't alter, redraw, distort or animate a mark, and don't recolour it beyond
  the variants its owner provides. One-colour versions are drawn in a single
  solid ink, as owners' one-colour artwork is.
- Follow each owner's brand guidelines; the catalogue records their URL where
  known (`provenance.guidelines_url`).
- Remove a mark on the owner's request: delete its files, set the entry back to
  a name tile and rebuild (`systems/README.md`, "Removing a mark").

Sources and licences:

- Most marks come from [Simple Icons](https://simpleicons.org) (npm
  `simple-icons`, pinned in `package.json`). Its SVG data is released under
  CC0-1.0; marks carry their owners' trademarks, and some carry their own
  licence, which the catalogue records. Marks whose recorded licence does not
  allow commercial use with adaptation, or which carry custom terms, are not
  used. Where a licence asks for credit (CC-BY), the catalogue flags
  `attribution_required`.
- Official files come from an owner's brand, press or media-kit page that
  offers the logo for download, or from an official icon programme for
  diagrams (Microsoft's Azure, Dynamics 365 and Power Platform icons; AWS
  Architecture Icons), whose terms limit them to architecture diagrams,
  training and documentation. Wikimedia Commons files carry their stated
  licence and trademark status. `systems/official/sources.json` records, per
  brand, why it is fetched or skipped; each original is kept untouched with a
  `provenance.json` (source URL, date retrieved, licence or terms, guidelines
  URL, allowed colour variants, whether a one-colour version is allowed, and
  its SHA-256). Brands whose terms forbid third-party logo use keep a name
  tile, and some owners set conditions that the files record (for example,
  less prominent than Firefly's branding, and only in interoperability
  diagrams).
- Systems without a clean source get a name tile: the name typeset by Firefly
  in Manrope (SIL Open Font License 1.1), clearly not the owner's logo.

## Third-party tools

The build uses open-source packages listed in `package.json` and
`requirements.txt`, each under its own licence. The FFmpeg binary comes from
the `ffmpeg-static` package and is used only to encode the kit's videos.

Contact: info@fireflysoftware.io
