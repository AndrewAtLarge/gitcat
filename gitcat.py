#!/usr/bin/env python3
r'''
git-cat
=======

*Herding a catalogue of git repositories*

----

Git-cat makes it possible to manage multiple git repositories from the command
line. Git-cat makes it possible to push and pull from multiple git repositories
to and from remote servers, such as bitbucket_ and github_, automatically
committing changes when necessary. As the aim of git-cat is to manage multiple
repositories simultaneously, the output from git commands is tailored to be
succinct and to the point.

Git-cat does not support all git commands and nor does it support the full
functionality of those git commands that it does support. Instead, it provides
a crude way of synchronising multiple repositories with remote servers. The
git-cat philosophy is to "do no harm" so, when possible, it uses dry-runs
before changing any repository and only makes actual changes to the repository
if the dry-run succeeds.  Any problems encountered by git-cat are printed to
the terminal.

----

Author
......

Andrew Mathas

git-cat Version 1.0

Copyright (C) 2018

GNU General Public License, Version 3, 29 June 2007

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License (GPL_) as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE.  See the GNU General Public License for more details.

.. _bitbucket: https://bitbucket.org/
.. _github: https://github.com
.. _GPL: http://www.gnu.org/licenses/gpl.html
.. _Python: https://www.python.org/

'''

# ---------------------------------------------------------------------------
# TODO:
#  - fix README and documentation
#  - debugging and testing...
#  - make "git cat git" command work
#  - make "git cat pull" first update the repository containing the gitcatrc file and
#     then reread it
#  - add a "git cat --set-as-defaults cmd [options]" option to set defaults
#     for a given command and then store the information into the gitcatrc
#     file. Will need to be clever to avoid code duplication...possibly add all
#     of the command-line options to the settings class and then use it to
#     automatically generate the command line options
#  - add options for sorting catalogue
#  - make status check that changes have been pushed
#  - add a fast option
#  - add exclude option

import argparse
import itertools
import os
import re
import shutil
import signal
import subprocess
import sys
import textwrap

# ---------------------------------------------------------------------------
# error messages and debugging


def error_message(err):
    r'''
    Print error message and exit.
    '''
    print('git cat error: {}'.format(err))
    sys.exit(1)


def debugging(message):
    """ print a debugging message if `debugging` is true"""
    if settings.DEBUGGING:
        print(message)


# ---------------------------------------------------------------------------
def graceful_exit(sig, frame):
    ''' exit gracefully on SIGINT and SIGTERM '''
    print('program terminated (signal {})'.format(sig))
    debugging('{}'.format(frame))
    sys.exit()


signal.signal(signal.SIGINT, graceful_exit)
signal.signal(signal.SIGTERM, graceful_exit)

# ---------------------------------------------------------------------------
# compiled regular expressions

# section in an ini file
ini_section = re.compile(r'^\[([a-zA-Z]*)\]$')

# [ahead 1], or [behind 1] or [ahead # 2, behind 1] in status
ahead_behind = re.compile(r'\[((ahead|behind) [0-9]+(, )?)+\]')

# list of files that have changed
files_changed = re.compile(r'([0-9]+ file(?:s|))(?: changed)')

# remove colons around works
colon = re.compile(r':([a-z]+):')

# ---------------------------------------------------------------------------
# settings
class Settings(dict):
    r"""
    A class for reading and saving the gitcat settings and supported git
    command line options.
    """
    DEBUGGING = False

    def __init__(self, ini_file, git_options_file):
        super().__init__()

        self.git_defaults = {}  # will hold non-standard git defaults
        self.prefix = os.environ['HOME']
        self.quiet = False
        self.dry_run = False

        # location of the gitcatrc file defaults to ~/.dotfiles/config/gitcatrc
        # and then to ~/.gitcatrc
        if os.path.isdir(os.path.expanduser('~/.dotfiles/config')):
            self.rc_file = os.path.expanduser('~/.dotfiles/config/gitcatrc')
        if not os.path.isfile(self.rc_file):
            self.rc_file = os.path.expanduser('~/.gitcatrc')

        self.read_init_file(ini_file)
        self.read_git_options(git_options_file)

    @staticmethod
    def doc_string(cmd):
        '''
        Return a sanitised version of the doc-string for the method `cmd` of
        `GitCat`. In particular, all code-blocks are removed.
        '''
        return textwrap.dedent(getattr(GitCat, cmd).__doc__)

    def add_git_options(self, command_parser):
        '''
        Generate all of the git-cat command options as parsers of `command_parser`
        '''
        for cmd in self.commands:
            command = command_parser.add_parser(
                cmd,
                help=self.commands[cmd]['description'],
                description=self.commands[cmd]['description'],
                formatter_class=argparse.RawTextHelpFormatter,
                epilog=self.doc_string(cmd)
            )
            for option in self.commands[cmd]:
                if option != 'description':
                    if 'short-option' in self.commands[cmd][option]:
                        options = self.commands[cmd][option].copy()
                        short_option = options['short-option']
                        del options['short-option']
                        debugging('short option = {}.'.format(short_option))
                        if short_option is None:
                            command.add_argument('--' + option, **options)
                        else:
                            command.add_argument('-' + short_option,
                                                 '--' + option, **options)
                    else:
                        command.add_argument('-' + option[:1], '--' + option,
                                             **self.commands[cmd][option])

            # finally, add the optional repository filter option
            if 'directory' not in self.commands[cmd]:
                command.add_argument(
                    dest='repositories',
                    type=str,
                    default='',
                    nargs='?',
                    help='optionally filter repositories for status')

            # add a quiet option
            command.add_argument(
                '-q', '--quiet',
                default=False,
                action='store_true',
                help='only print "important" messages')

    def read_init_file(self, ini_file):
        '''
        Read and store the information in the ini file
        '''
        with open(ini_file, 'r') as ini:
            for line in ini:
                key, val = [w.strip() for w in line.split('=')]
                if key != '':
                    if '.' in key:
                        command, option = key.split('.')
                        if not command in self.git_defaults:
                            self.git_defaults[command] = {}
                        self.git_defaults[command][option] = val
                    else:
                        setattr(self, '_' + key.lower(), val)

    def read_git_options(self, options_file):
        '''
        Read and store the information in the command-line options file
        '''
        self.commands = {}
        with open(options_file, 'r') as options:
            for line in options:
                match = ini_section.search(line.strip())
                if match:
                    # line is an ini section of the form: [command]
                    # set command and initialise to an empty dictionary
                    command = match.groups()[0]
                    self.commands[command] = {}

                elif not line.startswith('#') and '=' in line:

                    choices = [c.strip() for c in line.split('=')]
                    if len(choices) == 3:
                        # initial option line for current command which is
                        # of the form: opt = <help message> = <default value>
                        opt = choices[0]
                        default = choices[2]
                        option = dict(help=choices[1])
                        if opt.startswith('*'):
                            opt = opt[1:]
                            option['short-option'] = None

                        try:
                            option['default'] = eval(default)
                        except (NameError, SyntaxError, TypeError):
                            option['default'] = default.strip()
                        if isinstance(option['default'], bool):
                            option['action'] = 'store_{}'.format(
                                str(not option['default']).lower())
                        if isinstance(option['default'], str):
                            option['type'] = str

                        # dest could be overwritten later in the ini file
                        option['dest'] = 'git_' + opt.replace('-', '_')
                        self.commands[command][opt] = option
                    elif len(choices) == 2:
                        # description of command or extra specifications for the current option
                        if choices[0] == 'description':
                            self.commands[command]['description'] = choices[1]
                        else:
                            try:
                                self.commands[command][opt][choices[0]] = eval(choices[1])
                            except (NameError, SyntaxError, TypeError) as err:
                                self.commands[command][opt][choices[0]] = choices[1]
                    else:
                        error_message(
                            'syntax error in {} on the line\n {}'.format( options_file, line)
                        )

    def save_settings(self):
        r'''
        Return a string for setting the non-standard settings in the gitcatrc file
        '''
        save_settings = ''
        if self.prefix != os.environ['HOME']:
            save_settings += 'prefix = {}\n'.format(self.prefix)
        return save_settings

    def version(self):
        """ return gitcat version """
        return 'git cat version {}'.format(self._version)


file = lambda f: os.path.join(os.path.dirname(__file__), f)
settings = Settings(file('gitcat.ini'), file('git-options.ini'))


# ---------------------------------------------------------------------------
# running git commands using subprocess
class Git:
    """
    Container class for running a git command and printing an
    error message if necessary.

    Usage: Git(rep, command, options)

    where
     - rep     is the key for the repository being processed
     - command is the main git command being run
     - options are the options to the git commend

    The class that is return has attributes:
     - rep        the catalogue key for the respeoctory
     - returncode the return code from the subprocess command
     - output     the stdout and stderr output from the subprocess command
    """

    def __init__(self, rep, command, options=''):
        """ run a git command and wrap the return values for later use """
        git = subprocess.run(
            'git {} {}'.format(command, options).strip(),
            shell=True,
            capture_output=True)

        # store the output
        self.rep = rep
        self.returncode = git.returncode
        self.command = command + ' ' + options

        if self.returncode != 0:
            self.error_message = '{}: there was an error using git {} {}\n  {}\n'.format(
                rep,
                command,
                options,
                git.stderr.decode().strip().replace('\n', '\n  ').replace(
                    '\r', '\n  '),
            )
            debugging('{line}{err}{line}'.format(line='-' * 40, err=self.error_message))
            self.git_command_ok = False
        else:
            self.git_command_ok = True

        # output is indented two spaces and has no blank lines
        self.output = '\n'.join('  ' + lin.strip() for lin in (
            git.stdout.decode().replace('\r', '\n').strip().split('\n') +
            git.stderr.decode().replace('\r', '\n').strip().split('\n'))
                                if lin != '')
        debugging('{}\nstdout={}\nstderr={}'.format(self, git.stdout,
                                                    git.stderr))

    def __bool__(self):
        ''' return 'self.is_ok` '''
        return self.git_command_ok

    def __repr__(self):
        """ define a __repr__ method for debugging """
        return 'Git({})\n  rep={}, OK={}, returncode={}\n  output={}.'.format(
            self.command,
            self.rep,
            self.git_command_ok,
            self.returncode,
            self.output.replace('\n', '\n  '),
        )


# ---------------------------------------------------------------------------
class GitCat:
    r"""
    Usage: GitCat(options)

    A class for reading, accessing and storing details of the different git
    repositories. These are stored in `filename` in the form:

       directory1 = repository1
       directory2 = repository2
       ...

    Any lines without a key-value pair are ignored.
    """

    def __init__(self, options):
        self.gitcatrc = options.catalogue
        self.options = options
        self.prefix = options.prefix
        self.dry_run = False

        for opt in ['dry_run', 'quiet']:
            if hasattr(options, opt):
                setattr(self, opt, getattr(options, opt))
            if hasattr(options, 'git_'+opt):
                setattr(self, opt, getattr(self, opt) or getattr(options, 'git_'+opt))

        # read the catalogue from the rc file
        self.read_catalogue()

        # run corresponding command
        getattr(self, options.command)()

    def changed_files(self, rep):
        r'''
        Return list of files repository in the current directory that have
        changed.  We assume that we are in a git repository.
        '''
        return Git(rep, 'diff-index', '--name-only HEAD')

    def commit_repository(self, rep):
        r'''
        Commit the files in the repository in current working directory.
        The commit message is a list of the files being changed. Return
        the Git() record of the commit.
        '''
        debugging('\nCOMMIT rep=' + rep)
        changed_files = self.changed_files(rep)
        if changed_files and changed_files.output != '':
            commit_message = 'git cat: updating ' + changed_files.output
            commit = '--all --message="{}"'.format(commit_message)
            if self.dry_run:
                commit += ' --porcelain'
            return Git(rep, 'commit', commit)

        return changed_files

    def expand_path(self, dire):
        r'''
        Return the path to the directory `dire`, adding `self.prefix` if
        necessary.
        '''
        return dire if dire.startswith('/') else os.path.join(self.prefix, dire)

    def is_git_repository(self, dire):
        r'''
        Return `True` if `dire` is a git repository and `False` otherwise. As
        part of testing for a repository the current working directory is also
        changed to `dire`.
        '''
        debugging('\nCHECKING for git dire={}'.format(dire))
        if os.path.isdir(dire):
            os.chdir(dire)
            rep = dire.replace(self.prefix + '/', '')
            is_git = Git(rep, 'rev-parse', '--is-inside-work-tree')
            return is_git.returncode == 0 and 'true' in is_git.output

        return False

    def list_catalogue(self, listing):
        r'''
        Return a string that lists the repositories in the catalogue. If
        `listing` is `False` and the repository does not exist then the
        separator is an exclamation mark, otherwise it is an equals sign.
        '''
        return '\n'.join('{dire:<{max}} {sep} {rep}'.format(
            dire=dire,
            rep=self.catalogue[dire],
            sep='=' if listing or self.
            is_git_repository(self.expand_path(dire)) else '!',
            max=self.max) for dire in self.repositories())

    def process_options(self, default_options=''):
        r'''
           Set the command line options starting with `default_options` and
           then checking the command list options against the list of options
           in `options_list`
        '''
        options = default_options
        for option in vars(self.options):
            if option.startswith('git_'):
                opt = option[4:].replace('_', '-')
                val = getattr(self.options, option)
                if val is True:
                    options += ' --' + opt
                elif isinstance(val, list):
                    options += ' --{}={}'.format(opt, ','.join(val))
                elif isinstance(val, str):
                    options += ' --{}={}'.format(opt, val)
                else:
                    debugging('option {}={} ignored'.format(option, val))
        return options

    def read_catalogue(self):
        r'''
        Read the catalogue of git repositories to sync. These are stored in the
        form:

           directory1 = repository1
           directory2 = repository2
           ...

        and then put into the dictionary self.catalogue with the directory as
        the key. Any lines that do not contain an equal sign are ignored.
        '''
        self.catalogue = {}
        try:
            with open(self.gitcatrc, 'r') as catalogue:
                for line in catalogue:
                    if ' = ' in line:
                        dire, rep = line.split(' = ')
                        dire = dire.strip()
                        if dire in self.catalogue:
                            error_message(
                                '{} appears in the catalogue more than once!'.
                                format(dire))
                        elif dire.lower == 'prefix':
                            self.prefix = rep.strip()
                        else:
                            self.catalogue[dire] = rep.strip()
        except (FileNotFoundError, OSError):
            error_message(
                'there was a problem reading the catalogue file {}'.format(
                    self.gitcatrc))

        # set the maximum length of a catalogue key
        try:
            self.max = max(len(dire) for dire in self.repositories()) + 1
        except ValueError:
            self.max = 0

    def save_catalogue(self):
        r'''
        Save the catalogue of git repositories to sync
        '''
        with open(self.gitcatrc, 'w') as catalogue:
            catalogue.write(
                '# List of git repositories to sync using gitcat\n\n')
            catalogue.write(settings.save_settings())
            catalogue.write(self.list_catalogue(listing=True) + '\n')

    def short_path(self, dire):
        r'''
        Return the shortened path to the directory `dire` obtained by removing `self.prefix`
        if necessary.
        '''
        debugging('prefix = {}.'.format(self.prefix))
        debugging('dire = {}, prefixed={}'.format(dire,
                                                  dire.startswith(
                                                      self.prefix)))
        return dire[len(self.prefix) + 1:] if dire.startswith(
            self.prefix) else dire

    def repositories(self):
        ''' return the list of repositories to iterate over by
            filtering by options.repositories
        '''
        # if there is no filter then return the catalogue keys
        if not hasattr(self.options, 'repositories'):
            return self.catalogue.keys()

        repositories = re.compile(self.options.repositories)
        return filter(repositories.search, self.catalogue.keys())

    # ---------------------------------------------------------------------------
    # messages
    # ---------------------------------------------------------------------------

    def message(self, message, ending=None):
        r'''
        If `self.quiet` is `True` then print `message` to stdout, with `ending`
        as the, well, ending. If `self.quiet` is `False` then do nothing.
        '''
        if not self.quiet:
            debugging('-' * 40)
            print(message, end=ending)
            debugging('-' * 40)

    def quiet_message(self, message, ending=None):
        r'''
        If `self.quiet` is `False` then print `message` to stdout, with `ending`
        as the, well, ending. If `self.quiet` is `True` then do nothing.
        '''
        if self.quiet:
            debugging('-' * 40)
            print(message, end=ending)
            debugging('-' * 40)

    def rep_message(self, rep, message='', quiet=True, ending=None):
        r'''
        If `self.quiet` is `True` then print `message` to stdout, with `ending`
        as the, well, ending. If `self.quiet` is `False` then do nothing.
        '''
        debugging(
            'rep message: quiet={}, self.quiet={} and quietness={}\n{}'.format(
                quiet, self.quiet, not (quiet and self.quiet), '-' * 40))
        if not (quiet and self.quiet):
            print('{:<{max}} {}'.format(rep, message, max=self.max), 
                  end=ending)
            debugging('-' * 40)

    # ---------------------------------------------------------------------------
    # Now implement the git cat commands that are available from the command line
    # The doc-strings for this methods become part of help text in the manual.
    # In particular, any Example blocks become code blocks.
    # ---------------------------------------------------------------------------

    def add(self):
        r'''
        Add the current repository to the catalogue stored in gitcatrc. An
        error is returned if the current directory is not a git repository, if
        it is a git repository but has no remote or if the repository is
        already in the catalogue.

        '''
        if self.options.git_directory is None:
            dire = self.short_path(os.getcwd())
        else:
            dire = self.short_path(os.path.expanduser(self.options.repository))
        dire = self.expand_path(dire)

        if not (os.path.isdir(dire) and self.is_git_repository(dire)):
            error_message('{} not a git repository'.format(dire))

        # find the root directory for the repository and the remote URL`
        os.chdir(dire)
        root = Git(dire, 'root')
        if not root:
            error_message('{} is not a git repository:\n  {}'.format(
                dire, root.output))

        rep = Git(dire, 'remote', 'get-url --push origin')
        if not rep:
            error_message(
                'Unable to find remote repository for {}'.format(dire)
            )

        dire = self.short_path(root.output.strip())
        rep = rep.output.strip()
        if dire in self.catalogue:
            # give an error if repository is already in the catalogue
            error_message(
                'the git repository in {} is already in the catalogue'.format(
                    dire))
        else:
            # add current directory to the repository and save
            self.catalogue[dire] = rep
            self.save_catalogue()
            self.message('Adding {} to the catalogue'.format(dire))

            # check to see if the gitcatrc is in a git repository and, if so,
            # add a commit message
            catdir = os.path.dirname(self.gitcatrc)
            if self.is_git_repository(catdir):
                Git(
                    dire, 'commit', '--all --message="{}"'.format(
                        'Adding {} to gitcatrc'.format(dire)))

    def branch(self):
        r'''
        Run `git branch --verbose` in selected repositories in the
        catalogue. This gives a summary of the status of the branches in the
        repositories managed by git cat.

        Example:
            > git cat branch Code
            Code/Prog1
              python3 6c2fcd5 Putting out the washing
            Code/Prog2
              master  2d2614e [ahead 1] Making some important changes
            Code/Prog3        already up to date
            Code/Prog4        already up to date
            Code/Prog5
              branch1 14fc541 Adding braid method to tableau
              * branch2       68480a4 git cat: updating   doc/README.rst
              master             862e2f4 Adding good stuff
            Code/Prog6            already up to date

        '''
        # need to use -q to stop output being printed to stderr, but then we
        # have to work harder to extract information about the pull
        options = self.process_options('--verbose')
        for rep in self.repositories():
            debugging('\nBRANCH ' + rep)
            dire = self.expand_path(rep)
            if self.is_git_repository(dire):
                pull = Git(rep, 'branch', options)
                if pull:
                    if '\n' not in pull.output:
                        self.rep_message(rep, 'already up to date')
                    else:
                        self.rep_message(rep,
                                         pull.output[pull.output.index('\n'):])
            else:
                self.rep_message(rep, 'not on system')

    def ls(self):
        r'''
        List the repositories managed by git cat, together with the location of
        their remote repository.

        Example:
            > git cat ls
            Code/Prog1    = git@bitbucket.org:AndrewsBucket/prog1.git
            Code/Prog2    = git@bitbucket.org:AndrewsBucket/prog2.git
            Code/Prog3    = git@bitbucket.org:AndrewsBucket/prog3.git
            Code/Prog4    = git@bitbucket.org:AndrewsBucket/prog4.git
            Code/GitCat   = gitgithub.com:AndrewMathas/gitcat.git
            Notes/Life    = gitgithub.com:AndrewMathas/life.git

        '''
        print(self.list_catalogue(listing=False))

    def commit(self):
        r'''
        Commit all of the repositories in the catalogue where files have
        changed. The work is actually done by `self.commit_repository`, which
        commits only one repository, since other methods need to call this as
        well.
        '''
        for rep in self.repositories():
            debugging('\nCOMMITTING ' + rep)
            dire = self.expand_path(rep)
            if self.is_git_repository(dire):
                self.commit_repository(rep)

    def diff(self):
        r'''
        Run git diff with various options on the repositories in the
        catalogue.
        '''
        options = self.process_options()
        options += ' HEAD'
        for rep in self.repositories():
            debugging('\nDIFFING ' + rep)
            dire = self.expand_path(rep)
            if self.is_git_repository(dire):
                diff = Git(rep, 'diff' 'options')
                if diff:
                    if diff.output != '':
                        self.rep_message(rep, diff.output, quiet=False)
                    else:
                        self.rep_message(rep, 'up to date')

    def fetch(self):
        r'''
        Run through all repositories and update them if their directories
        already exist on this computer
        '''
        # need to use -q to stop output being printed to stderr, but then we
        # have to work harder to extract information about the pull
        options = self.process_options('-q --progress')
        for rep in self.repositories():
            debugging('\nFETCHING ' + rep)
            dire = self.expand_path(rep)
            if self.is_git_repository(dire):
                pull = Git(rep, 'fetch', options)
                if pull:
                    if pull.output == '':
                        self.rep_message(rep, 'already up to date')
                    else:
                        self.rep_message(rep, pull.output)
            else:
                self.rep_message(rep, 'not on system')

    def git(self, commands):
        r''' Run git commands on every repository in the catalogue '''
        git_command = '{}'.format(' '.join(cmd for cmd in commands))
        for rep in self.repositories():
            debugging('\nGITTING ' + rep)
            dire = self.expand_path(rep)
            if self.is_git_repository(dire):
                print('Repository = {}, command = {}'.format(rep, git_command))
                Git(git_command)

    def install(self):
        r'''
        Install listed repositories from the catalogue.

        If a directory exists but is not a git repository then initialise the
        repository and fetch from the remote.
        '''
        for rep in self.repositories():
            debugging('\nINSTALLING ' + rep)
            dire = self.expand_path(rep)
            if os.path.exists(dire):
                if os.path.exists(os.path.join(dire, '.git')):
                    self.rep_message(
                        'git repository {} already exists'.format(dire))
                else:
                    # initialise current repository and fetch from remote
                    Git(rep, 'init')
                    Git(rep,
                        'remote add origin {}'.format(self.catalogue[rep]))
                    Git(rep, 'fetch origin')
                    Git(rep, 'checkout -b master --track origin/master')

            else:
                self.rep_message(rep, 'installing')
                parent = os.path.dirname(dire)
                os.makedirs(parent, exist_ok=True)
                os.chdir(parent)
                if not self.dry_run:
                    install = Git(
                        rep, 'clone', '--quiet {rep} {dire}'.format(
                            rep=self.catalogue[rep],
                            dire=os.path.basename(dire)))
                    if install:
                        self.message(' - done!')
            if not (self.dry_run or self.is_git_repository(dire)):
                self.rep_message(
                    rep, 'not a git repository!?'.format(rep), quiet=False)

    def pull(self):
        r'''
        Run through all repositories and update them if their directories
        already exist on this computer. Unless the  `--quiet` option is used, 
        a message is printed to give the summarise the status of the
        repository.

        Example:
            > git cat pull
            Code/Prog1    already up to date
            Code/Prog2    already up to date
            Code/Prog3    already up to date
            Code/Prog4    already up to date
            Code/GitCat   already up to date
            Notes/Life    already up to date

        '''
        # need to use -q to stop output being printed to stderr, but then we
        # have to work harder to extract information about the pull
        options = self.process_options('-q --progress')
        for rep in self.repositories():
            debugging('\nPULLING ' + rep)
            dire = self.expand_path(rep)
            if self.is_git_repository(dire):
                pull = Git(rep, 'pull', options)
                if pull:
                    if pull.output == '':
                        self.rep_message(rep, 'already up to date')
                    else:
                        self.rep_message(
                            rep,
                            'pulling\n' + '\n'.join(
                                lin for lin in pull.output.split('\n')
                                if 'Compressing' not in lin),
                            quiet=False)
            else:
                self.rep_message(rep, 'repository not installed')

    def push(self):
        r'''
        Run through all installed repositories and push them to their remote
        repositories. Any uncommitted repository with local changes will be
        committed and the commit message listing the files that have changed.
        Unless the `-quiet` option is used, a summary of the status of
        each repository is printed with each push.

        Example:
            > git cat pull
            Code/Prog1    already up to date
            Code/Prog2    already up to date
            Code/Prog3    already up to date
            Code/Prog4    already up to date
            Code/GitCat   already up to date
            Notes/Life    already up to date

        '''
        options = self.process_options('--porcelain --follow-tags')
        for rep in self.repositories():
            debugging('\nPUSHING ' + rep)
            dire = self.expand_path(rep)
            if self.is_git_repository(dire):
                debugging('Continuing with push')
                commit = self.commit_repository(rep)
                if commit:
                    if commit.output != '':
                        self.rep_message(rep, 'commit\n' + commit.output)
                    push = Git(rep, 'push', options + ' --dry-run')
                    if push:
                        if '[up to date]' in push.output:
                            self.rep_message(rep, 'up to date')
                        elif not self.dry_run:
                            push = Git(rep, 'push', options)

                            if push:
                                if push.output.startswith(
                                        '  To ') and push.output.endswith(
                                            'Done'):
                                    if commit.output == '' and 'up to date' not in commit.output:
                                        self.rep_message(
                                            rep, 'pushed\n' + push.output)
                                    else:
                                        self.message(
                                            push.output.split('\n')[0])
                                else:
                                    if commit.output == '' and 'up to date' not in commit.output:
                                        self.rep_message(
                                            rep, 'pushed\n' + push.output)
                                    else:
                                        self.message(push.output)

            else:
                self.rep_message(rep, 'not on system')

    def remove(self):
        r'''
        Remove the directory `dire` from the catalogue of repositories to
        sync. An error is given if got cat is not managing this repository.
        '''
        if self.options.git_directory is None:
            dire = self.short_path(os.getcwd())
        else:
            dire = self.short_path(os.path.expanduser(self.options.repository))
        dire = self.expand_path(dire)

        if not (rep in self.catalogue and self.is_git_repository(dire)):
            error_message('unknown repository {}'.format(dire))

        del self.catalogue[rep]
        self.message('Removing {} from the catalogue'.format(dire))
        self.save_catalogue()

        if self.options.git_everything:
            # remove directory
            self.message('Removing directory {}'.format(dire))
            shutil.rmtree(dire)

            # check to see if the gitcatrc is in a git repository and, if so,
            # add a commit message
            catdir = os.path.dirname(self.gitcatrc)
            if self.is_git_repository(catdir):
                Git(
                    dire, 'commit', '--all --message "{}"'.format(
                        'Removing {} from gitcatrc'.format(dire)))

    def status(self):
        r'''
        Print a summary of the status of all of the repositories in the
        catalogue. The name is slightly misleading as this command does not
        just run `git status` on each repository and, instead, it queries the
        remote repositories to determine whether each repository is ahead or
        behind the remote repository.

        Example:
            > git cat status
            Code/Prog1    up to date
            Code/Prog2    ahead 1
            Code/Prog3    = git@bitbucket.org:AndrewsBucket/prog3.git
            Code/Prog4    up to date= git@bitbucket.org:AndrewsBucket/prog4.git
            Code/GitCat   behind 1
            Notes/Life    up to date= gitgithub.com:AndrewMathas/life.git
        '''
        status_options = self.process_options('--porcelain --short --branch')
        diff_options = '--shortstat --no-color'

        for rep in self.repositories():
            debugging('\nSTATUS for {}'.format(rep))
            dire = self.expand_path(rep)
            if self.is_git_repository(dire):

                # update with remote, unless local is true
                remote = self.options.git_local or Git(rep, 'remote', 'update')

                if remote:
                    # use status to work out relative changes
                    status = Git(rep, 'status', status_options)
                    if status:
                        changes = ahead_behind.search(status.output)
                        changes = '' if changes is None else changes.group(
                        )[1:-1]

                        if '\n' in status.output:
                            status.output = status.output[status.output.
                                                          index('\n') + 1:]
                        elif status.output.startswith('  ##'):
                            status.output = ''

                        # use diff to work out which files have changed
                        diff = Git(rep, 'diff', diff_options)
                        changed = ''
                        if diff:
                            changed = files_changed.search(diff.output)
                            changed = '' if changed is None else 'uncommitted changes in ' + changed.groups(
                            )[0]

                        debugging('changes = {}\nchanged={}\nstatus={}'.format(
                            changes, changed, status.output))

                        if changes != '':
                            changed += changes if changed == '' else ', ' + changes

                        if status.output != '':
                            self.rep_message(
                                rep,
                                changed + '\n' + status.output,
                                quiet=False)
                        elif changed != '':
                            self.rep_message(rep, changed, quiet=False)
                        else:
                            self.rep_message(rep, 'up to date')

            else:
                self.rep_message(rep, 'not on system')


# ---------------------------------------------------------------------------
SUPPRESS = '==SUPPRESS=='

class GitCatHelpFormatter(argparse.HelpFormatter):
    ''' 
    Override help formatter so that we can print a list of ythe possible
    commands together with a quick summary of them
    '''

    def _format_action(self, action):
        if isinstance(action, argparse._SubParsersAction):
            # inject new class variable for subcommand formatting
            subactions = action._get_subactions()
            invocations = [
                self._format_action_invocation(a) for a in subactions
            ]
            self._subcommand_max_length = max(len(i) for i in invocations)

        if isinstance(action, argparse._SubParsersAction._ChoicesPseudoAction):
            # format subcommand help line
            subcommand = self._format_action_invocation(action)  # type: str
            width = self._subcommand_max_length+2
            help_text = ""
            if action.help:
                help_text = self._expand_help(action)
            return "  {:{width}}  {}\n".format(subcommand, help_text, width=width)

        elif isinstance(action, argparse._SubParsersAction):
            # process subcommand help section
            message = ''
            for subaction in action._get_subactions():
                message += self._format_action(subaction)
            return message

        return super()._format_action(action)

    def _metavar_formatter(self, action, default_metavar):
        if action.metavar is not None:
            result = action.metavar
        elif action.choices is not None:
            result = '<command> [options]'
        else:
            result = default_metavar

        def new_format(tuple_size):
            if isinstance(result, tuple):
                return result

            return (result, ) * tuple_size

        return new_format


# ---------------------------------------------------------------------------
def setup_command_line_parser():
    '''
    Return parsers for the command line options and the commands.
    The function is used to parse the command-line options an to automatically
    generate the documentation from setup.py
    '''
    # allow the command line options to change the DEBUGGING flag
    global settings

    # set parse the command line options using argparse
    parser = argparse.ArgumentParser(
        #add_help=False,
        description='Simultaneously synchronise multiple local and remote git repositories',
        formatter_class=GitCatHelpFormatter,
        prog='git cat',
    )

    # ---------------------------------------------------------------------------
    # catalogue options
    # ---------------------------------------------------------------------------
    parser.add_argument(
        '-c',
        '--catalogue',
        type=str,
        default=settings.rc_file,
        help='specify the catalogue of git repositories (default: {})'.format(settings.rc_file))
    parser.add_argument(
        '-p',
        '--prefix',
        type=str,
        default=settings.prefix,
        help='Prefix directory name containing all repositories')
    parser.add_argument(
        '-q',
        '--quiet',
        action='store_true',
        default=settings.quiet,
        help='Print messages only if repository changes')
    # parser.add_argument(
    #     '-s',
    #     '--set-as-default',
    #     action='store_true',
    #     default=False,
    #     help='use the current options for <command> as the default')

    # options suppressed from help
    parser.add_argument(
        '--debugging',
        action='store_true',
        default=False,
        help=argparse.SUPPRESS)
    parser.add_argument(
        '-v',
        '--version',
        action='version',
        version=settings.version(),
        help=argparse.SUPPRESS)

    # ---------------------------------------------------------------------------
    # add catalogue commands using settings and the git-options.ini file
    # ---------------------------------------------------------------------------
    command_parser = parser.add_subparsers(
        title='Commands',
        help='Subcommand to run',
        dest='command')
    settings.add_git_options(command_parser)
    parser._optionals.title = 'Optional arguments'
    return parser, command_parser


def main():
    r'''
    Parse command line options and then run git cat
    '''
    parser, command_parser = setup_command_line_parser()
    options = parser.parse_args()
    settings.DEBUGGING = options.debugging

    if options.command is None:
        parser.print_help()
        sys.exit(1)

    GitCat(options)

# ---------------------------------------------------------------------------
if __name__ == '__main__':
    main()
