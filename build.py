#!/usr/bin/env python3
import argparse, getpass, os, platform, random, re, semver, shutil, sys
from os.path import join
from utils import *

# The default Windows Server Core base image tag
# (See <https://hub.docker.com/r/microsoft/windowsservercore/> for a list of valid tags)
DEFAULT_WINDOWS_BASETAG = '1803'

if __name__ == '__main__':
	
	# Create our logger to generate coloured output on stderr
	logger = Logger(prefix='[build.py] ')
	
	# Our supported command-line arguments
	parser = argparse.ArgumentParser()
	parser.add_argument('release', help='UE4 release to build, in semver format (e.g. 4.19.0)')
	parser.add_argument('--linux', action='store_true', help='Build Linux container images under Windows')
	parser.add_argument('--rebuild', action='store_true', help='Rebuild images even if they already exist')
	parser.add_argument('--dry-run', action='store_true', help='Print `docker build` commands instead of running them')
	parser.add_argument('--no-ue4cli', action='store_true', help='Don\'t build the conan-ue4cli image')
	parser.add_argument('--no-package', action='store_true', help='Don\'t build the ue4-package image')
	parser.add_argument('--random-memory', action='store_true', help='Use a random memory limit for Windows containers')
	parser.add_argument('--nvidia', action='store_true', help='Build GPU-enabled images for NVIDIA Docker under Linux')
	parser.add_argument('-isolation', default=None, help='Set the isolation mode to use for Windows containers (process or hyperv)')
	parser.add_argument('-basetag', default=DEFAULT_WINDOWS_BASETAG, help='Windows Server Core base image tag to use for Windows containers (default is ' + DEFAULT_WINDOWS_BASETAG + ')')
	
	# If no command-line arguments were supplied, display the help message and exit
	if len(sys.argv) < 2:
		parser.print_help()
		sys.exit(0)
	
	# Parse the supplied command-line arguments and validate the specified version string
	args = parser.parse_args()
	try:
		ue4Version = semver.parse(args.release)
		ue4VersionStr = semver.format_version(ue4Version['major'], ue4Version['minor'], ue4Version['patch'])
		if ue4Version['major'] != 4 or ue4Version['prerelease'] != None:
			raise Exception()
	except:
		logger.error('Error: invalid UE4 release number "{}", full semver format required (e.g. "4.19.0")'.format(args.release))
		sys.exit(1)
	
	# Determine if we are building Windows or Linux containers
	containerPlatform = 'windows' if platform.system() == 'Windows' and args.linux == False else 'linux'
	platformArgs = []
	if containerPlatform == 'windows':
		
		# Set the memory limit Docker flags
		limit = 8.0 if args.random_memory == False else random.uniform(8.0, 10.0)
		platformArgs = ['-m', '{:.2f}GB'.format(limit)]
		
		# Set the isolation mode Docker flags
		if args.isolation != None:
			platformArgs.append('-isolation=' + args.isolation)
		
		# Provide the user with feedback so they are aware of the Windows-specific values being used
		logger.info('WINDOWS CONTAINER SETTINGS', False)
		logger.info('Isolation mode:           {}'.format(args.isolation if args.isolation != None else 'default'), False)
		logger.info('Base OS image tag:        ' + args.basetag, False)
		logger.info('Memory limit:             {:.2f}GB'.format(limit), False)
		logger.info('Detected max image size:  {:.0f}GB\n'.format(DockerUtils.maxsize()), False)
		
	elif containerPlatform == 'linux':
		
		# Determine if we are building GPU-enabled container images
		linuxBaseImage = 'ubuntu:18.04'
		if args.nvidia == True:
			logger.info('Building GPU-enabled images for use with NVIDIA Docker.\n', False)
			linuxBaseImage = 'nvidia/opengl:1.0-glvnd-devel-ubuntu18.04'
	
	# If we are building Windows containers, ensure the Docker daemon is configured correctly
	if containerPlatform == 'windows' and DockerUtils.maxsize() < 200.0:
		logger.error('SETUP REQUIRED:')
		logger.error('The max image size for Windows containers must be set to at least 200GB.')
		logger.error('See the Microsoft documentation for configuration instructions:')
		logger.error('https://docs.microsoft.com/en-us/visualstudio/install/build-tools-container#step-4-expand-maximum-container-disk-size')
		sys.exit(1)
	
	# Determine if we are performing a dry run
	if args.dry_run == True:
		
		# Don't bother prompting the user for any credentials
		logger.info('Performing a dry run, `docker build` commands will be printed and not executed.', False)
		username = ''
		password = ''
		
	else:
		
		# Retrieve the Git username and password from the user
		print('Enter the Git credentials that will be used to clone the UE4 repo')
		username = input("Username: ")
		password = getpass.getpass("Password: ")
	
	# Start the HTTP credential endpoint as a child process and wait for it to start
	endpoint = CredentialEndpoint(username, password)
	endpoint.start()
	
	# Create the builder instance to build the Docker images
	contextRoot = join(os.path.dirname(os.path.abspath(__file__)), 'dockerfiles')
	builder = ImageBuilder(contextRoot, 'adamrehn/', containerPlatform, logger)
	
	# If we are building Windows containers, copy the required DirectSound and OpenGL DLL files from the host system
	if containerPlatform == 'windows':
		for dll in ['dsound.dll', 'opengl32.dll', 'glu32.dll']:
			shutil.copy2(join(os.environ['SystemRoot'], 'System32', dll), join(builder.context('ue4-build-prerequisites'), dll))
	
	try:
		
		# Build the UE4 build prerequisites image
		prereqsArgs = ['--build-arg', 'BASETAG=' + args.basetag] if containerPlatform == 'windows' else ['--build-arg', 'BASEIMAGE=' + linuxBaseImage]
		builder.build('ue4-build-prerequisites', 'latest', platformArgs + prereqsArgs, args.rebuild, args.dry_run)
		
		# Build the UE4 source image
		ue4SourceArgs = ['--build-arg', 'GIT_TAG={}-release'.format(ue4VersionStr)]
		builder.build('ue4-source', ue4VersionStr, platformArgs + ue4SourceArgs + endpoint.args(), args.rebuild, args.dry_run)
		
		# Build the UE4 build image
		ue4BuildArgs = ['--build-arg', 'TAG={}'.format(ue4VersionStr)]
		builder.build('ue4-build', ue4VersionStr, platformArgs + ue4BuildArgs, args.rebuild, args.dry_run)
		
		# Build the conan-ue4cli image for 4.19.0 or newer, unless requested otherwise by the user
		buildUe4Cli = ue4Version['minor'] >= 19 and args.no_ue4cli == False
		if buildUe4Cli == True:
			builder.build('conan-ue4cli', ue4VersionStr, platformArgs + ue4BuildArgs, args.rebuild, args.dry_run)
		else:
			logger.info('UE4 version less than 4.19.0 or user specified `--no-ue4cli`, skipping conan-ue4cli image build.')
		
		# Build the UE4 packaging image (for packaging Shipping builds of projects), unless requested otherwise by the user
		if buildUe4Cli == True and args.no_package == False:
			builder.build('ue4-package', ue4VersionStr, platformArgs + ue4BuildArgs, args.rebuild, args.dry_run)
		else:
			logger.info('Not building conan-ue4cli or user specified `--no-package`, skipping ue4-package image build.')
		
		# Stop the HTTP server
		endpoint.stop()
	
	except:
		
		# One of the images failed to build
		endpoint.stop()
		sys.exit(1)
