# Industrial Liturgy

A heavy machinery groove with a semi-liturgical haze: 84 BPM, E Phrygian, 8 bars, looping.
By Garyck Arntzen (https://x.com/_mintydoge).

Every sound is synthesised: AIDAW's built-in `foundry` (sub kick, pistons, anvil, slam, hiss, ratchet,
gears, chain, steam, scrape, sheet metal), `bell` and `synth` instruments, plus the optional
Horror Engine VST3 layer. No samples are used, so there are no third-party licences to track.

    python gen.py                                   # writes the .pdaw scripts
    aidaw new industrial-liturgy
    aidaw -p industrial-liturgy run build_industrial.pdaw
    aidaw -p industrial-liturgy run iterate_industrial.pdaw
    aidaw -p industrial-liturgy run horror_engine.pdaw   # only where Horror Engine is installed
    aidaw -p industrial-liturgy render --formats wav,mp3
