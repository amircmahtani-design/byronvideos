# Third-party software and model notices

This repository does not include the Kokoro weights or a private user's artwork.
Dependencies retain their own licenses; their installed distributions include the
authoritative license texts. This notice does not relicense the user's application.

- [Kokoro inference code](https://github.com/hexgrad/kokoro) and
  [Kokoro-82M model](https://huggingface.co/hexgrad/Kokoro-82M): Apache-2.0.
  The model card describes training data and associated attributions. Use the
  official repository, not similarly named commercial websites.
- [Misaki](https://github.com/hexgrad/misaki): Apache-2.0.
- [PyTorch](https://github.com/pytorch/pytorch): BSD-style license and bundled
  dependency notices.
- [Transformers](https://github.com/huggingface/transformers): Apache-2.0.
- [spaCy](https://github.com/explosion/spaCy) and its English language pipeline:
  retain the respective MIT/model distribution notices.
- [Pillow](https://github.com/python-pillow/Pillow): HPND-style license.
- [FFmpeg](https://ffmpeg.org/legal.html): LGPL/GPL depending on build configuration.
  This prototype invokes a system FFmpeg executable; the tested build uses GPL
  components including libx264. If distributing a worker image/binary, comply with
  that actual build's notices/source obligations; generated videos are a separate
  matter from redistribution of the encoder.
- The bundled, fingerprinted **DejaVu Sans** font has its complete upstream
  Bitstream Vera/DejaVu terms in `style-guide/fonts/LICENSE-DejaVu.txt`.

The website's Playfair Display and Cormorant Garamond names are suggested brand
continuity only; the local UI does not fetch external font files. The video caption
font is the bundled DejaVu Sans until a different licensed font is explicitly
reviewed and versioned in the style guide.

All geometric test illustrations are original deterministic QA fixtures. They are
labelled synthetic and must not be presented as the user's chosen classical art.
