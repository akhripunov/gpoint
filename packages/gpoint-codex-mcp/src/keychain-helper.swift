import Foundation
import Security

let arguments = CommandLine.arguments
guard arguments.count == 4 else { exit(2) }
let operation = arguments[1]
let service = arguments[2]
let account = arguments[3]
let query: [String: Any] = [
    kSecClass as String: kSecClassGenericPassword,
    kSecAttrService as String: service,
    kSecAttrAccount as String: account
]

func fail(_ status: OSStatus) -> Never {
    fputs("Keychain operation failed (\(status)).\n", stderr)
    exit(1)
}

switch operation {
case "set":
    let value = FileHandle.standardInput.readDataToEndOfFile()
    guard !value.isEmpty else { exit(2) }
    let attributes: [String: Any] = [
        kSecValueData as String: value,
        kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
    ]
    let status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
    if status == errSecItemNotFound {
        var item = query
        attributes.forEach { item[$0.key] = $0.value }
        let addStatus = SecItemAdd(item as CFDictionary, nil)
        if addStatus != errSecSuccess { fail(addStatus) }
    } else if status != errSecSuccess {
        fail(status)
    }
case "get":
    var request = query
    request[kSecReturnData as String] = true
    request[kSecMatchLimit as String] = kSecMatchLimitOne
    var result: CFTypeRef?
    let status = SecItemCopyMatching(request as CFDictionary, &result)
    if status == errSecItemNotFound { exit(4) }
    guard status == errSecSuccess, let data = result as? Data else { fail(status) }
    FileHandle.standardOutput.write(data)
case "delete":
    let status = SecItemDelete(query as CFDictionary)
    if status != errSecSuccess && status != errSecItemNotFound { fail(status) }
default:
    exit(2)
}
