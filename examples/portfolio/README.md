# halo: working example

Ask a question against a small note pack; inspect retrieved evidence and step through a prepared cue.

**[Open the demo](https://lolstar123.github.io/halo/)** · [Calculation / workflow code](model.mjs) · [Checks](model.test.mjs)

![Example output](preview.png)

## Run it

From the repository root, with Python 3 and Node.js 22:

```sh
python -m http.server 8000 --directory examples/portfolio
```

Open http://localhost:8000. Change an input, or edit the JSON fixture, then export the computed result as JSON or CSV.

```sh
node --test examples/portfolio/model.test.mjs
```

## What it does

Transcribe the question, combine it with selected context and prepare a response. The overlay breaks that response into readable sentences so the next useful point stays in view.

## Scope and source

Offline evidence retrieval and sentence navigation. The full repository contains the audio/AI application; this example makes no model request.

Public context.py, sentence_reader.py, live_sentences.py and demo.py.

`model.mjs` is the small public implementation. `app.mjs` connects its inputs and outputs to the browser. No package install or network key is needed to run the example. GitHub Pages runs the same files after the checks pass.
