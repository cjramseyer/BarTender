class Tap {
  final int id;
  final int number;
  final String label;
  final int? kegId;
  final String notes;

  const Tap({
    required this.id,
    required this.number,
    required this.label,
    this.kegId,
    required this.notes,
  });

  factory Tap.fromJson(Map<String, dynamic> json) => Tap(
        id: json['id'] as int,
        number: json['number'] as int? ?? 0,
        label: json['label'] as String? ?? '',
        kegId: json['keg_id'] as int?,
        notes: json['notes'] as String? ?? '',
      );
}
