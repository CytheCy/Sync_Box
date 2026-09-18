Name:           sync-box
Version:        1.0.0
Release:        1%{?dist}
Summary:        Conservative two-way synchronization between a local folder and Box
License:        LicenseRef-Not-Provided
URL:            https://github.com/CytheCy/Sync_Box
Source0:        %{url}/archive/v%{version}.tar.gz#/%{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3dist(setuptools) >= 77
BuildRequires:  python3dist(wheel)
BuildRequires:  python3dist(pip) >= 19
BuildRequires:  desktop-file-utils
BuildRequires:  libappstream-glib
Requires:       python3dist(boxsdk) >= 10
Requires:       python3dist(boxsdk) < 11
Requires:       python3dist(pyside6) >= 6.6
Requires:       systemd
Recommends:     nodejs-npm

%description
Sync_Box safely synchronizes a local folder with Box using a verified SQLite
baseline. This package includes the command-line interface, a PySide6 system
tray application, and disabled-by-default user service and timer units.

The official Box CLI 4.6 or newer must be installed separately because Fedora
does not currently ship it as an RPM. Sync_Box never stores OAuth credentials.

%prep
%autosetup -n %{name}-%{version}

%generate_buildrequires
%pyproject_buildrequires -x gui

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files sync_box

install -Dpm 0644 packaging/sync-box.desktop \
    %{buildroot}%{_datadir}/applications/sync-box.desktop
install -Dpm 0644 packaging/sync-box-autostart.desktop \
    %{buildroot}%{_sysconfdir}/xdg/autostart/sync-box.desktop
install -Dpm 0644 src/sync_box/resources/icons/sync-box.svg \
    %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/sync-box.svg
install -Dpm 0644 packaging/io.github.sync_box.SyncBox.metainfo.xml \
    %{buildroot}%{_metainfodir}/io.github.sync_box.SyncBox.metainfo.xml
install -Dpm 0644 packaging/sync-box.service \
    %{buildroot}%{_userunitdir}/sync-box.service
install -Dpm 0644 packaging/sync-box.timer \
    %{buildroot}%{_userunitdir}/sync-box.timer

%check
%pyproject_check_import
desktop-file-validate packaging/sync-box.desktop
desktop-file-validate packaging/sync-box-autostart.desktop
appstream-util validate-relax --nonet packaging/io.github.sync_box.SyncBox.metainfo.xml

%files -f %{pyproject_files}
%doc README.md config.example.toml
%{_bindir}/sync-box
%{_bindir}/sync-box-gui
%{_datadir}/applications/sync-box.desktop
%config(noreplace) %{_sysconfdir}/xdg/autostart/sync-box.desktop
%{_datadir}/icons/hicolor/scalable/apps/sync-box.svg
%{_metainfodir}/io.github.sync_box.SyncBox.metainfo.xml
%{_userunitdir}/sync-box.service
%{_userunitdir}/sync-box.timer

%changelog
* Fri Sep 18 2026 Sync_Box Maintainers <noreply@example.invalid> - 1.0.0-1
- Add the native Qt tray application and Fedora user-service packaging
