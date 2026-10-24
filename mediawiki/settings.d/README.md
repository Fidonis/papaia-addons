# Site-specific settings

Every `*.php` file in this directory is loaded at the end of the add-on's
`LocalSettings.php`, in alphabetical order, so it can override any setting
the add-on makes — a logo, an extra extension shipped in the image, a
different skin default.

Point `MW_SETTINGS_DIR` at a directory outside the add-on checkout to keep
these files across add-on updates. Files that `require` secrets should read
them with `getenv()` rather than embedding them.
