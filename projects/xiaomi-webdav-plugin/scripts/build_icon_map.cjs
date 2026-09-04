const fs = require('fs');
const path = require('path');
const web = path.resolve(__dirname, '../web');
const names = ['reload','folder','close','plus','up','copy','link','edit','trash','upload','download','play','stop','transfer'];
const assets = Object.fromEntries(names.map(name => [name, 'data:image/png;base64,' + fs.readFileSync(path.join(web, 'assets', name + '.png')).toString('base64')]));
fs.writeFileSync(path.join(web, 'icon-assets.js'), '// Generated from the bundled Radix PNG icons.\nwindow.WEBDAV_ICON_ASSETS = Object.freeze(' + JSON.stringify(assets) + ');\n');
