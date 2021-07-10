#!/bin/bash
wget -qO- https://raw.githubusercontent.com/nvm-sh/nvm/v0.38.0/install.sh | bash &&\
export NVM_DIR="$([ -z "${XDG_CONFIG_HOME-}" ] && printf %s "${HOME}/.nvm" || printf %s "${XDG_CONFIG_HOME}/nvm")"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh" &&\
nvm install v14.17.3 && nvm use v14.17.3 && nvm alias default v14.17.3 && nvm install-latest-npm
mkdir build; cd build && git clone https://github.com/jesec/flood.git &&\
cd flood &&\
npm install &&\
npm run buil d&&\
npm --global install &&\
npm --global run build
