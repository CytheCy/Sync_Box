#!/usr/bin/bash
set -euo pipefail

project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
version=$(sed -n 's/^version = "\([^"]*\)"/\1/p' "$project_root/pyproject.toml" | head -n1)
build_root=${SYNC_BOX_RPMBUILD_ROOT:-"$project_root/build/rpmbuild"}
source_dir="$build_root/SOURCES"

if ! command -v rpmbuild >/dev/null; then
    echo "rpmbuild is required (Fedora package: rpm-build)" >&2
    exit 2
fi

mkdir -p "$source_dir" "$build_root/BUILD" "$build_root/BUILDROOT" \
    "$build_root/RPMS" "$build_root/SRPMS" "$build_root/SPECS"

archive="$source_dir/sync-box-$version.tar.gz"
tar --create --gzip --file "$archive" \
    --transform "s,^,sync-box-$version/," \
    --exclude='.git' --exclude='.*venv*' --exclude='build' --exclude='dist' \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='src/sync_box.egg-info' \
    -C "$project_root" pyproject.toml README.md config.example.toml packaging src

cp "$project_root/packaging/sync-box.spec" "$build_root/SPECS/sync-box.spec"
rpmbuild -ba \
    --define "_topdir $build_root" \
    "$build_root/SPECS/sync-box.spec"
