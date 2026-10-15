# Exoloom visual assets

reference.png is the user-supplied artwork, copied without pixel changes.
engine.js and lettering.js reuse the existing portable portal export;
the shaders, textures, preset and metallic glyph renderer are retained.
The animation adapter adds a frame pacing gate; rendering math is unchanged.
The private portal export/build tooling is excluded from this publication checkout.

The prebuilt UI assets remain available independently of the private deployment stack.

icon.svg is the code-native black-hole mark (shadow, photon ring, lensed halo, Doppler-bright disk); icon.png is its raster export.
icon-16.svg is the hand-tuned browser-tab variant (thicker strokes, no swirl, stars or blur); keep the two in step.

Portal source SHA-256: `575d9d89c4e3422d9dba543af02ec002f39a0212fa46e4191c1508e826f85447`

Reference SHA-256: `0594d5d05dc7ad4ec1288de763ac98b7e4667ef242ff738ee453aee00b6ac101`

The engine retains its upstream Three.js / Mustache / raytracer comments.
Orbitron and Teko are bundled with their SIL Open Font Licenses from [Google Fonts](https://github.com/google/fonts/tree/main/ofl).
