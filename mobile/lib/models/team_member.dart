class TeamMember {
  final String id;
  final String name;

  const TeamMember({required this.id, required this.name});

  factory TeamMember.fromJson(Map<String, dynamic> json) {
    return TeamMember(
      id: json['id'] as String,
      name: json['name'] as String,
    );
  }
}
