# Deid-echo Documentation

![assets/img/logo.png](assets/img/logo.png)

This site documents deid-echo, an echocardiogram ultrasound focused fork of https://www.github.com/pydicom/deid. The included recipes are developed and validated only on echo ultrasound DICOM studies. Parallel batch runners live in deidecho_run/ and are covered in the user docs.

It remains part of the pydicom family of tools and uses the same underlying interfaces.

## Setup

 1. Install Jekyll locally. For Ruby, I recommend rbenv.
 2. Install Jekyll dependencies with `bundle install`
 3. To serve the development server run `bundle exec jekyll serve`

## Folders Included
If you are not familiar with the structure of a Jekyll site, here is a quick overview:

 - [_config.yml](_config.yml) is the primary configuration file for the site. Variables in this file render as {{ site.var }} in the various html includes and templates.
 - [_layouts](_layouts) are base html templates for pages
 - [_includes](_includes) are snippets of html added to layouts
 - [pages](pages) are generic pages (e.g., changelog) that are not considered docs
 - [_docs](_docs) is a collection of folders that get rendered into the docs sidebar and pages
 - [assets](assets) includes all static assets
 - [_data](_data) has different data files (they can be in .yml or .csv) to render into the site.
