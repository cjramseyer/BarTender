class Keg {
  final int id;
  final String name;
  final String type;
  final String size;
  final String status;
  final String brewery;
  final String abv;
  final String notes;
  final String filledDate;
  final int percentFull;

  const Keg({
    required this.id,
    required this.name,
    required this.type,
    required this.size,
    required this.status,
    required this.brewery,
    required this.abv,
    required this.notes,
    required this.filledDate,
    required this.percentFull,
  });

  factory Keg.fromJson(Map<String, dynamic> json) => Keg(
        id: json['id'] as int,
        name: json['name'] as String? ?? '',
        type: json['type'] as String? ?? '',
        size: json['size'] as String? ?? '',
        status: json['status'] as String? ?? 'empty',
        brewery: json['brewery'] as String? ?? '',
        abv: json['abv'] as String? ?? '',
        notes: json['notes'] as String? ?? '',
        filledDate: (json['filled_date'] as String?) ?? (json['purchased_date'] as String?) ?? '',
        percentFull: (json['percent_full'] as num?)?.round() ??
            ((json['status'] as String? ?? 'empty') == 'full'
                ? 100
                : ((json['status'] as String? ?? 'empty') == 'in_use' ? 50 : 0)),
      );

  Color get statusColor {
    switch (status) {
      case 'full':
        return const Color(0xFF4CAF50);
      case 'in_use':
        return const Color(0xFF2196F3);
      case 'empty':
        return const Color(0xFF9E9E9E);
      case 'cleaning':
        return const Color(0xFFFF9800);
      case 'retired':
        return const Color(0xFFF44336);
      default:
        return const Color(0xFF9E9E9E);
    }
  }
}
