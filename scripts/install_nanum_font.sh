#!/usr/bin/env bash
set -euo pipefail

font_dir=/usr/share/fonts/truetype/nanum
fonts_ready() {
    [[ -r "$font_dir/NanumGothic.ttf" && -r "$font_dir/NanumGothicBold.ttf" ]]
}

if fonts_ready; then
    exit 0
fi

apt_options=(-o Acquire::Retries=0 -o Acquire::http::Timeout=8 -o Acquire::https::Timeout=8)
if sudo apt-get "${apt_options[@]}" update -qq &&
    sudo apt-get "${apt_options[@]}" install -y -qq fonts-nanum &&
    fonts_ready; then
    exit 0
fi

# Ubuntu resolute의 동일 패키지를 공식 다운로드 페이지에 등재된 미러에서 받는다.
echo "APT 글꼴 설치 실패. Ubuntu 미러의 동일 패키지를 확인합니다."
work_dir="$(mktemp -d)"
trap 'rm -r -- "$work_dir"' EXIT
archive="$work_dir/fonts-nanum.deb"
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --connect-timeout 10 --max-time 45 --output "$archive" \
    https://mirrors.kernel.org/ubuntu/pool/universe/f/fonts-nanum/fonts-nanum_20250212-1_all.deb
echo "3cd2d18fc67c616c3a2e037ece0eb6aece414755025e2ddeec4f6e828783dc76  $archive" | sha256sum --check
dpkg-deb --extract "$archive" "$work_dir/unpacked"
for font in NanumGothic.ttf NanumGothicBold.ttf; do
    sudo install -D -m 0644 "$work_dir/unpacked$font_dir/$font" "$font_dir/$font"
done
fonts_ready
