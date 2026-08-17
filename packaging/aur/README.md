# AUR packaging

`PKGBUILD` here installs the base viewer only - `stk_viewer/` plus a
`/usr/bin/stk-viewer` wrapper and a desktop entry. It deliberately does
**not** touch `patches/`: building the patched SuperTuxKart is a separate,
much heavier thing (clones and compiles the game), and stays something you
opt into per-machine via `install.py` or `patches/build.sh` directly, not
something an AUR package should do on install.

Built and installed for real against the pre-rename `v1.5.0` tag while
drafting this - `makepkg` succeeds, and the resulting package's
`/usr/bin/stk-viewer` (well, `/usr/bin/stk-minimap` at the time),
`--list`/`--gui`, and the `.desktop` entry all worked. `sha256sums` here is
currently `SKIP` as a placeholder: the `v1.6.0` tag this PKGBUILD points at
doesn't exist under the renamed repo yet. Run `updpkgsums` from this
directory once it's tagged and pushed, before submitting.

## Submitting it (not done yet - needs your AUR account)

This directory isn't the AUR repo itself; AUR packages live in their own
git remote (`ssh://aur@aur.archlinux.org/stk-viewer.git`), pushed to with
an SSH key registered to your AUR account. First time:

```bash
git clone ssh://aur@aur.archlinux.org/stk-viewer.git aur-stk-viewer
cp packaging/aur/PKGBUILD packaging/aur/.SRCINFO aur-stk-viewer/
cd aur-stk-viewer
git add PKGBUILD .SRCINFO
git commit -m "Initial import: stk-viewer 1.6.0"
git push
```

## Updating it for a new release

After tagging and releasing a new version here:

```bash
cd packaging/aur
# bump pkgver (and reset pkgrel=1) in PKGBUILD, then:
updpkgsums                        # refetches the new tag's tarball, updates sha256sums
makepkg --printsrcinfo > .SRCINFO
makepkg -f                        # sanity build before pushing
```

Then copy both files into the AUR clone, commit, and push, same as above. A
packaging-only change (no `pkgver` bump) just increments `pkgrel` instead.
