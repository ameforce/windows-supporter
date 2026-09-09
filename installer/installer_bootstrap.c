#define UNICODE
#define _UNICODE

#include <windows.h>
#include <shellapi.h>

#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>

#define TRAILER_SIZE 16
#define COMMAND_BUFFER_SIZE 32768
#define PATH_BUFFER_SIZE 32768

static const char PAYLOAD_MAGIC[8] = {'W', 'S', 'U', 'S', 'E', 'T', 'U', 'P'};

static int copy_wide_text(wchar_t *destination, size_t capacity, const wchar_t *source) {
    size_t length;

    if (destination == NULL || source == NULL || capacity == 0) {
        return 0;
    }
    length = wcslen(source);
    if (length >= capacity) {
        return 0;
    }
    memcpy(destination, source, (length + 1) * sizeof(wchar_t));
    return 1;
}

static int append_wide_text(
    wchar_t *destination,
    size_t capacity,
    size_t *length,
    const wchar_t *source
) {
    size_t source_length;

    if (destination == NULL || length == NULL || source == NULL) {
        return 0;
    }
    source_length = wcslen(source);
    if (*length > capacity - 1 || source_length > capacity - 1 - *length) {
        return 0;
    }
    memcpy(destination + *length, source, source_length * sizeof(wchar_t));
    *length += source_length;
    destination[*length] = L'\0';
    return 1;
}

static int append_wide_character(
    wchar_t *destination,
    size_t capacity,
    size_t *length,
    wchar_t value
) {
    if (destination == NULL || length == NULL || *length >= capacity - 1) {
        return 0;
    }
    destination[*length] = value;
    *length += 1;
    destination[*length] = L'\0';
    return 1;
}

static int append_quoted_argument(
    wchar_t *command,
    size_t capacity,
    size_t *length,
    const wchar_t *argument
) {
    const wchar_t *cursor;
    size_t backslashes = 0;

    if (!append_wide_character(command, capacity, length, L'"')) {
        return 0;
    }
    for (cursor = argument; *cursor != L'\0'; ++cursor) {
        if (*cursor == L'\\') {
            backslashes += 1;
            continue;
        }
        while (backslashes > 0) {
            if (!append_wide_character(command, capacity, length, L'\\')) {
                return 0;
            }
            backslashes -= 1;
        }
        if (*cursor == L'"') {
            if (!append_wide_character(command, capacity, length, L'\\')) {
                return 0;
            }
        }
        if (!append_wide_character(command, capacity, length, *cursor)) {
            return 0;
        }
    }
    while (backslashes > 0) {
        if (!append_wide_character(command, capacity, length, L'\\')) {
            return 0;
        }
        backslashes -= 1;
    }
    return append_wide_character(command, capacity, length, L'"');
}

static int append_command_argument(
    wchar_t *command,
    size_t capacity,
    size_t *length,
    const wchar_t *argument
) {
    if (*length > 0 && !append_wide_character(command, capacity, length, L' ')) {
        return 0;
    }
    return append_quoted_argument(command, capacity, length, argument);
}

static int normalize_legacy_value(
    wchar_t *destination,
    size_t capacity,
    const wchar_t *raw_value
) {
    const wchar_t *start = raw_value;
    size_t end;
    size_t source_index = 0;
    size_t output_length = 0;

    if (destination == NULL || raw_value == NULL || capacity == 0) {
        return 0;
    }
    while (start[0] == L'"' || (start[0] == L'\\' && start[1] == L'"')) {
        start += start[0] == L'\\' ? 2 : 1;
    }
    end = wcslen(start);
    while (end > 0 && start[end - 1] == L'"') {
        end -= 1;
        if (end > 0 && start[end - 1] == L'\\') {
            end -= 1;
        }
    }
    while (output_length + 1 < capacity && source_index < end) {
        if (start[source_index] == L'"') {
            source_index += 1;
            continue;
        }
        destination[output_length] = start[source_index];
        source_index += 1;
        output_length += 1;
    }
    destination[output_length] = L'\0';
    return output_length > 0;
}

static int starts_with_switch(const wchar_t *argument, const wchar_t *prefix) {
    size_t prefix_length = wcslen(prefix);
    return _wcsnicmp(argument, prefix, prefix_length) == 0;
}

static int append_normalized_switch(
    wchar_t *command,
    size_t capacity,
    size_t *length,
    const wchar_t *switch_name,
    const wchar_t *raw_value
) {
    wchar_t value[PATH_BUFFER_SIZE];
    wchar_t normalized[PATH_BUFFER_SIZE];

    if (!normalize_legacy_value(value, sizeof(value) / sizeof(value[0]), raw_value)) {
        return 1;
    }
    if (_snwprintf(
            normalized,
            sizeof(normalized) / sizeof(normalized[0]) - 1,
            L"%ls%ls",
            switch_name,
            value
        ) < 0) {
        return 0;
    }
    normalized[(sizeof(normalized) / sizeof(normalized[0])) - 1] = L'\0';
    return append_command_argument(command, capacity, length, normalized);
}

static int default_log_path(wchar_t *destination, size_t capacity) {
    DWORD length = GetEnvironmentVariableW(L"LOCALAPPDATA", destination, (DWORD)capacity);
    size_t current_length;

    if (length == 0 || length >= capacity) {
        length = GetTempPathW((DWORD)capacity, destination);
        if (length == 0 || length >= capacity) {
            return 0;
        }
    }
    current_length = wcslen(destination);
    if (current_length > 0 && destination[current_length - 1] != L'\\') {
        if (!append_wide_text(destination, capacity, &current_length, L"\\")) {
            return 0;
        }
    }
    if (!append_wide_text(
            destination,
            capacity,
            &current_length,
            L"windows-supporter"
        ) || (!CreateDirectoryW(destination, NULL) && GetLastError() != ERROR_ALREADY_EXISTS)) {
        return 0;
    }
    if (!append_wide_text(destination, capacity, &current_length, L"\\release-installer-legacy.log")) {
        return 0;
    }
    return 1;
}

static int copy_payload_to_temp(
    const wchar_t *self_path,
    wchar_t *payload_path,
    size_t payload_path_capacity
) {
    HANDLE source = INVALID_HANDLE_VALUE;
    HANDLE destination = INVALID_HANDLE_VALUE;
    LARGE_INTEGER file_size;
    LARGE_INTEGER payload_offset;
    LARGE_INTEGER seek_offset;
    DWORD trailer_read = 0;
    DWORD temp_path_length;
    unsigned char trailer[TRAILER_SIZE];
    uint64_t payload_size;
    wchar_t temp_directory[MAX_PATH];
    wchar_t temp_file[MAX_PATH];
    unsigned char buffer[256 * 1024];
    DWORD bytes_read;
    DWORD bytes_written;
    int success = 0;

    temp_file[0] = L'\0';

    source = CreateFileW(self_path, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (source == INVALID_HANDLE_VALUE || !GetFileSizeEx(source, &file_size) || file_size.QuadPart < TRAILER_SIZE) {
        goto cleanup;
    }
    seek_offset.QuadPart = -TRAILER_SIZE;
    if (!SetFilePointerEx(source, seek_offset, NULL, FILE_END) ||
        !ReadFile(source, trailer, TRAILER_SIZE, &trailer_read, NULL) ||
        trailer_read != TRAILER_SIZE || memcmp(trailer, PAYLOAD_MAGIC, sizeof(PAYLOAD_MAGIC)) != 0) {
        goto cleanup;
    }
    memcpy(&payload_size, trailer + sizeof(PAYLOAD_MAGIC), sizeof(payload_size));
    if (payload_size == 0 || payload_size > (uint64_t)(file_size.QuadPart - TRAILER_SIZE)) {
        goto cleanup;
    }
    payload_offset.QuadPart = file_size.QuadPart - TRAILER_SIZE - (LONGLONG)payload_size;

    temp_path_length = GetTempPathW((DWORD)(sizeof(temp_directory) / sizeof(temp_directory[0])), temp_directory);
    if (temp_path_length == 0 || temp_path_length >= sizeof(temp_directory) / sizeof(temp_directory[0]) ||
        GetTempFileNameW(temp_directory, L"wsu", 0, temp_file) == 0) {
        goto cleanup;
    }
    DeleteFileW(temp_file);
    destination = CreateFileW(temp_file, GENERIC_WRITE, 0, NULL, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL);
    if (destination == INVALID_HANDLE_VALUE) {
        goto cleanup;
    }
    if (!SetFilePointerEx(source, payload_offset, NULL, FILE_BEGIN)) {
        goto cleanup;
    }
    while (payload_size > 0) {
        DWORD requested = payload_size > sizeof(buffer) ? (DWORD)sizeof(buffer) : (DWORD)payload_size;
        if (!ReadFile(source, buffer, requested, &bytes_read, NULL) || bytes_read == 0 ||
            !WriteFile(destination, buffer, bytes_read, &bytes_written, NULL) || bytes_written != bytes_read) {
            goto cleanup;
        }
        payload_size -= bytes_read;
    }
    if (!copy_wide_text(payload_path, payload_path_capacity, temp_file)) {
        goto cleanup;
    }
    success = 1;

cleanup:
    if (destination != INVALID_HANDLE_VALUE) {
        CloseHandle(destination);
    }
    if (source != INVALID_HANDLE_VALUE) {
        CloseHandle(source);
    }
    if (!success && temp_file[0] != L'\0') {
        DeleteFileW(temp_file);
    }
    return success;
}

static int build_payload_command(
    int argc,
    wchar_t **argv,
    const wchar_t *payload_path,
    wchar_t *command,
    size_t command_capacity
) {
    size_t command_length = 0;
    int has_log = 0;
    int index;
    wchar_t log_path[PATH_BUFFER_SIZE];

    command[0] = L'\0';
    if (!append_command_argument(command, command_capacity, &command_length, payload_path)) {
        return 0;
    }
    for (index = 1; index < argc; ++index) {
        if (starts_with_switch(argv[index], L"/DIR=")) {
            if (!append_normalized_switch(command, command_capacity, &command_length, L"/DIR=", argv[index] + 5)) {
                return 0;
            }
        } else if (starts_with_switch(argv[index], L"/LOG=")) {
            if (!append_normalized_switch(command, command_capacity, &command_length, L"/LOG=", argv[index] + 5)) {
                return 0;
            }
            has_log = 1;
        } else if (!append_command_argument(command, command_capacity, &command_length, argv[index])) {
            return 0;
        }
    }
    if (!has_log && default_log_path(log_path, sizeof(log_path) / sizeof(log_path[0]))) {
        if (!append_normalized_switch(command, command_capacity, &command_length, L"/LOG=", log_path)) {
            return 0;
        }
    }
    return 1;
}

static DWORD launch_payload(const wchar_t *command_line, const wchar_t *working_directory) {
    STARTUPINFOW startup_info;
    PROCESS_INFORMATION process_info;
    wchar_t mutable_command[COMMAND_BUFFER_SIZE];
    DWORD exit_code = 1;

    if (!copy_wide_text(mutable_command, sizeof(mutable_command) / sizeof(mutable_command[0]), command_line)) {
        return exit_code;
    }
    ZeroMemory(&startup_info, sizeof(startup_info));
    startup_info.cb = sizeof(startup_info);
    ZeroMemory(&process_info, sizeof(process_info));
    if (!CreateProcessW(
            NULL,
            mutable_command,
            NULL,
            NULL,
            FALSE,
            CREATE_NO_WINDOW,
            NULL,
            working_directory,
            &startup_info,
            &process_info
        )) {
        return exit_code;
    }
    WaitForSingleObject(process_info.hProcess, INFINITE);
    GetExitCodeProcess(process_info.hProcess, &exit_code);
    CloseHandle(process_info.hThread);
    CloseHandle(process_info.hProcess);
    return exit_code;
}

int main(void) {
    wchar_t self_path[PATH_BUFFER_SIZE];
    wchar_t payload_path[PATH_BUFFER_SIZE];
    wchar_t command[COMMAND_BUFFER_SIZE];
    LPWSTR *argv = NULL;
    int argc = 0;
    DWORD self_length;
    DWORD exit_code;

    argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (argv == NULL) {
        return 1;
    }
    self_length = GetModuleFileNameW(NULL, self_path, (DWORD)(sizeof(self_path) / sizeof(self_path[0])));
    if (self_length == 0 || self_length >= sizeof(self_path) / sizeof(self_path[0]) ||
        !copy_payload_to_temp(self_path, payload_path, sizeof(payload_path) / sizeof(payload_path[0])) ||
        !build_payload_command(argc, argv, payload_path, command, sizeof(command) / sizeof(command[0]))) {
        LocalFree(argv);
        return 1;
    }
    exit_code = launch_payload(command, NULL);
    DeleteFileW(payload_path);
    LocalFree(argv);
    return (int)exit_code;
}
