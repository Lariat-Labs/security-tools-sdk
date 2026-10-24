using 'main.bicep'

// Copy to a local *.bicepparam (git-ignored) or pass values on the command line.
param prefix = 'agw'
param publisherEmail = 'platform-team@your-org.example'
param gatewayAppId = '00000000-0000-0000-0000-0000000000a0' // from entra.sh
param runtimeImage = '' // set on the second deploy
