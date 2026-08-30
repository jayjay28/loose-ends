import SwiftUI

/// Who to write to when a thread needs a message.
///
/// From the field notes: "if it needs to contact someone attached to the
/// thread, it knows who to contact." A thread the user *declares* carries no
/// evidence, so there was nobody to infer a recipient from — the writer had to
/// invent one or refuse. This is the user answering the question directly.
///
/// The list is ordered by traffic, not alphabetically: an A-Z list of every
/// handle that ever sent a message is mostly delivery robots and 2FA codes
/// with a few humans buried in it.
struct ContactPicker: View {
    @Binding var selection: Person?

    @Environment(\.syncService) private var syncService
    @Environment(\.dismiss) private var dismiss
    @State private var people: [Person] = []
    @State private var query = ""
    @State private var loading = true

    var body: some View {
        NavigationStack {
            List {
                if selection != nil {
                    Button(role: .destructive) {
                        selection = nil
                        dismiss()
                    } label: {
                        Label("No one", systemImage: "person.slash")
                    }
                }
                if loading {
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text("Loading people…")
                    }
                    .foregroundStyle(Theme.inkFaint)
                } else if people.isEmpty {
                    Text(query.isEmpty ? "No people yet." : "No one matching “\(query)”.")
                        .foregroundStyle(Theme.inkFaint)
                }
                ForEach(people) { person in
                    Button {
                        selection = person
                        dismiss()
                    } label: {
                        HStack {
                            VStack(alignment: .leading, spacing: 1) {
                                Text(person.displayName)
                                    .foregroundStyle(Theme.ink)
                                if let handle = person.handle {
                                    Text(handle)
                                        .font(.system(size: 11))
                                        .foregroundStyle(Theme.inkFaint)
                                }
                            }
                            Spacer()
                            if person.id == selection?.id {
                                Image(systemName: "checkmark").foregroundStyle(Theme.brand)
                            }
                        }
                    }
                }
            }
            .searchable(text: $query, prompt: "Search people")
            .navigationTitle("Who to contact")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
            .task(id: query) { await load() }
        }
    }

    private func load() async {
        people = (try? await syncService.api.people(matching: query)) ?? []
        loading = false
    }
}
