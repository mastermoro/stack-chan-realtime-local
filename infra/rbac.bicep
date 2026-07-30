targetScope = 'resourceGroup'

param foundryAccountName string
param principalId string

resource foundry 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: foundryAccountName
}

// Foundry User. Microsoft recommends using the role definition ID while the role rename rolls out.
var foundryUserRoleDefinitionId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '53ca6127-db72-4b80-b1b0-d745d6d5456d'
)

resource roleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, principalId, foundryUserRoleDefinitionId)
  scope: foundry
  properties: {
    roleDefinitionId: foundryUserRoleDefinitionId
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}
