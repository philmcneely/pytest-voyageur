# Security

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private reporting: open the **Security** tab of this
repository and choose **Report a vulnerability**. Say what you found, how to reproduce it, and which version or commit.

You will get a reply, and a fix or a clear answer, as soon as the maintainer can manage. This is a small project with one
maintainer: there is no service level agreement.

## What is in scope

The plugin edits files in your working tree and sends data to a service you configure, so these matter most:

- a way to make the client write outside the source roots, through a symlink, or anything but one clean locator expression;
- a way to make it evaluate text from the service or from a page as code;
- data that should never leave the machine going to the service (input values, cookies, storage state, screenshots, HTML);
- a bearer token sent somewhere it should not be (over `http://` to a non-localhost host without `HEAL_ALLOW_INSECURE=1`).

Problems in the Voyageur service itself belong in [its repository](https://github.com/philmcneely/voyageur).

## What the client promises

The README sections "How a heal goes" and "What leaves your machine" describe the checks. A report that shows one of them
failing is a security report.
