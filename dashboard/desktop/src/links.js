'use strict';
// Which addresses the app may open in the user's browser. Only this project's public GitHub pages.
const ALLOWED = [
  /^https:\/\/github\.com\/KasluBuilds\/CreatorStudio(\/[^\s]*)?$/,
  /^https:\/\/raw\.githubusercontent\.com\/KasluBuilds\/CreatorStudio\/[^\s]*$/,
];

function isAllowedLink(url) {
  return typeof url === 'string' && url.length < 2048 && ALLOWED.some((re) => re.test(url));
}

module.exports = { isAllowedLink };
