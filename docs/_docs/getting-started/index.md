---
title: Introduction
category: Getting Started
permalink: /getting-started/index.html
order: 1
---

Deid-echo cleans header and image data and filters based on headers. This fork is tuned for echocardiogram ultrasound pipelines; defaults and recipes have not been validated for other modalities. Parallel runner scripts in deidecho_run/ help batch large echo cohorts across multiple processes.

## Dicom Pipeline

A complete deid pipeline typically means some level of cleaning and filtering, and then saving final images.

 - [Loading Data]({{ site.baseurl }}/getting-started/dicom-loading): The starting point for any de-identification process is to read in your files.
 - [Configuration]({{ site.baseurl }}/getting-started/dicom-config): You next want to tell the software how to handle various fields.
 - [Get Identifiers]({{ site.baseurl }}/getting-started/dicom-get): A request for identifiers is a get, or extraction of data that can be modified.
 - [Clean Pixels]({{ site.baseurl }}/getting-started/dicom-pixels): Before you scrape headers, you might need to use them to flag images.
 - [Put Identifiers]({{ site.baseurl }}/getting-started/dicom-put): A "put" corresponds to putting cleaned headers back into the images.

If you are interested in other examples (with snippets of code) see our [examples]({{ site.baseurl }}/examples/) pages. For more detailed user documentation on writing recipes, see the [user documentation]({{ site.baseurl }}/user-docs/) base.
